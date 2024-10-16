# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math
import random
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Iterator, Literal, Optional, Union

import numpy as np
import torch
from lhotse import Recording
from lhotse.custom import CustomFieldMixin
from lhotse.cut import Cut
from lhotse.dataset.dataloading import resolve_seed
from lhotse.serialization import load_jsonl
from lhotse.utils import Pathlike

from nemo.collections.common.data.lhotse.nemo_adapters import expand_sharded_filepaths
from nemo.collections.common.data.prompt_fn import apply_prompt_format_fn, registered_prompt_format_fn
from nemo.collections.common.parts.preprocessing.manifest import get_full_path
from nemo.collections.common.tokenizers.aggregate_tokenizer import AggregateTokenizer, TokenizerWrapper

"""
Formattable: mixin class with data fields for prompt formatter outputs and method for 
applying prompt formatters to derived data types. 
"""


class Formattable:
    def __init__(self):
        self.input_ids: np.ndarray | torch.Tensor | None = None
        self.context_ids: np.ndarray | torch.Tensor | None = None
        self.answer_ids: np.ndarray | torch.Tensor | None = None
        self.mask: np.ndarray | torch.Tensor | None = None

    @property
    def input_length(self) -> int | None:
        if self.context_ids is None:
            return None
        return self.context_ids.shape[0]

    @property
    def output_length(self) -> int | None:
        if self.answer_ids is None:
            return None
        return self.answer_ids.shape[0]

    @property
    def total_length(self) -> int | None:
        if self.input_ids is None:
            return None
        return self.input_ids.shape[0]

    def apply_prompt_format(self, prompt) -> "Formattable":
        ans = apply_prompt_format_fn(self, prompt)
        self.input_ids = ans["input_ids"]
        self.context_ids = ans["context_ids"]
        self.answer_ids = ans["answer_ids"]
        self.mask = ans["mask"]
        return self


"""
TextExample: data types, file parser, default prompt formatting logic.
"""


@dataclass
class TextExample(Formattable, CustomFieldMixin):
    """
    Represents a single text example. Useful e.g. for language modeling.
    """

    text: str
    language: str | None = None
    tokens: Optional[np.ndarray] = None
    custom: dict = None

    def tokenize(self, tokenizer: TokenizerWrapper) -> "TextExample":
        self.tokens = np.asarray(tokenizer(self.text, self.language))
        return self


@dataclass
class LhotseTextAdapter:
    """
    ``LhotseTextAdapter`` is used to read a text file and wrap
    each line into a ``TextExample``.
    """

    paths: Union[Pathlike, list[Pathlike]]
    language: str | None = None
    shuffle_shards: bool = False
    shard_seed: Union[int, Literal["trng", "randomized"]] = "trng"

    def __post_init__(self):
        self.paths = expand_sharded_filepaths(self.paths)

    def __iter__(self) -> Iterator[TextExample]:
        paths = self.paths
        if self.shuffle_shards:
            seed = resolve_seed(self.shard_seed)
            random.Random(seed).shuffle(paths)
        for path in paths:
            with open(path) as f:
                for line in f:
                    yield TextExample(line, language=self.language)


@registered_prompt_format_fn(TextExample)
def default_text_example_prompt_format_fn(example: TextExample, prompt):
    # It doesn't really make sense to prompt format a single line text example,
    # but we implement some default logic for the sake of completeness.
    # The default logic here is to treat the whole example as an assistant turn,
    # so that the mask is all set to true for the training loss.
    return prompt.encode_dialog(
        [
            {"role": prompt.OUTPUT_ROLE, "slots": {"message": example.text}},
        ]
    )


"""
SourceTargetTextExample: data types, file parser, default prompt formatting logic.
"""


@dataclass
class SourceTargetTextExample(Formattable, CustomFieldMixin):
    """
    Represents a pair of text examples. Useful e.g. for sequence-to-sequence tasks.
    Supports a ``question`` field, used as the prompt for LLM.
    """

    source: TextExample
    target: TextExample
    question: TextExample | None = None
    custom: dict = None

    def tokenize(self, tokenizer: TokenizerWrapper) -> "SourceTargetTextExample":
        self.source = self.source.tokenize(tokenizer)
        self.target = self.target.tokenize(tokenizer)
        if self.question is not None:
            self.question = self.question.tokenize(tokenizer)
        return self


@dataclass
class LhotseTextPairAdapter:
    """
    ``LhotseTextAdapter`` is used to read a tuple of N text files
    (e.g., a pair of files with translations in different languages)
    and wrap them in a ``TextExample`` object to enable dataloading
    with Lhotse together with training examples in audio modality.

    Provide ``questions_path`` to enable randomly sampling lines with questions.
    """

    source_paths: Union[Pathlike, list[Pathlike]]
    target_paths: Union[Pathlike, list[Pathlike]]
    source_language: str | None = None
    target_language: str | None = None
    questions_path: Pathlike = None
    questions_language: str = None
    shuffle_shards: bool = False
    shard_seed: Union[int, Literal["trng", "randomized"]] = "trng"

    def __post_init__(self):
        ASSERT_MSG = "Both source and target must be a single path or lists of paths"
        if isinstance(self.source_paths, (str, Path)):
            assert isinstance(self.target_paths, (str, Path)), ASSERT_MSG
        else:
            assert isinstance(self.source_paths, list) and isinstance(self.target_paths, list), ASSERT_MSG
            assert len(self.source_paths) == len(
                self.target_paths
            ), f"Source ({len(self.source_paths)}) and target ({len(self.target_paths)}) path lists must have the same number of items."
        self.source_paths = expand_sharded_filepaths(self.source_paths)
        self.target_paths = expand_sharded_filepaths(self.target_paths)

    def __iter__(self) -> Iterator[SourceTargetTextExample]:
        seed = resolve_seed(self.shard_seed)
        rng = random.Random(seed)
        paths = list(zip(self.source_paths, self.target_paths))
        if self.shuffle_shards:
            rng.shuffle(paths)
        questions = None
        if self.questions_path is not None:
            with open(self.questions_path) as f:
                questions = [q.strip() for q in f]
        for source_path, target_path in paths:
            with open(source_path) as fs, open(target_path) as ft:
                for ls, lt in zip(fs, ft):
                    yield SourceTargetTextExample(
                        source=TextExample(ls.strip(), language=self.source_language),
                        target=TextExample(lt.strip(), language=self.target_language),
                        question=(
                            TextExample(rng.choice(questions), language=self.questions_language)
                            if questions is not None
                            else None
                        ),
                    )


@registered_prompt_format_fn(SourceTargetTextExample)
def default_src_tgt_prompt_format_fn(example: SourceTargetTextExample, prompt):
    if example.question is not None:
        ctx = f"{example.question.text} {example.source.text}"
    else:
        ctx = example.source.text
    return prompt.encode_dialog(
        [
            {"role": "user", "slots": {"message": ctx}},
            {"role": prompt.OUTPUT_ROLE, "slots": {"message": example.target.text}},
        ]
    )


"""
NeMoSFTExample: data types, file parser, default prompt formatting logic.
"""


@dataclass
class NeMoSFTExample(Formattable, CustomFieldMixin):
    data: dict
    language: str | None = None
    metadata: dict | None = None
    custom: dict = None


@registered_prompt_format_fn(NeMoSFTExample)
def default_sft_prompt_format_fn(example: NeMoSFTExample, prompt):
    if "system" in example.data and example.data["system"]:
        raise RuntimeError(
            f"Default prompt format for NeMoSFTExample doesn't support 'system' prompt. "
            f"Please specialize the prompt_format_fn for PromptFormatter of type {prompt}"
        )
    return prompt.encode_dialog(
        [
            {"role": "user" if turn["from"] == "User" else prompt.OUTPUT_ROLE, "slots": {"message": turn["value"]}}
            for turn in example.data["conversations"]
        ]
    )


@dataclass
class NeMoSFTJsonlAdapter:
    """
    ``NeMoSFTJsonlAdapter`` is used to read a NeMo LM SFT Chat JSONL file and yield objects of type
    ``NeMoSFTExample`` that can be sampled with Lhotse.

    We expect the following schema (contained in a single line per example)::

        {
            "conversations": [
                {
                    "value": str,
                    "from": "User" | "Assistant",
                    "canonical_form": str,
                    "label": str | null
                },
                ...
            ],
            "mask": "User" | "Assistant",
            "system": str,
            "dataset": str,
            "category": str,
        }
    """

    paths: Union[Pathlike, list[Pathlike]]
    language: str | None = None
    shuffle_shards: bool = False
    shard_seed: Union[int, Literal["trng", "randomized"]] = "trng"

    def __post_init__(self):
        self.paths = expand_sharded_filepaths(self.paths)

    def __iter__(self) -> Iterator[NeMoSFTExample]:
        paths = self.paths
        if self.shuffle_shards:
            seed = resolve_seed(self.shard_seed)
            random.Random(seed).shuffle(paths)
        for path in paths:
            for data in load_jsonl(path):
                yield NeMoSFTExample(data, language=self.language)


"""
NeMoMultimodalConversation: data types, file parser, default prompt formatting logic.
"""


@dataclass
class TextTurn:
    value: str
    role: str


@dataclass
class AudioTurn:
    cut: Cut
    role: str
    audio_locator_tag: str


@dataclass
class NeMoMultimodalConversation(Formattable, CustomFieldMixin):
    id: str
    turns: list[TextTurn | AudioTurn]
    token_equivalent_duration: float = None
    custom: dict = None

    @property
    def input_length(self) -> int | None:
        if self.context_ids is None:
            return None
        extra = _compute_num_audio_tokens(self, "context")
        return self.context_ids.shape[0] + extra

    @property
    def output_length(self) -> int | None:
        if self.answer_ids is None:
            return None
        extra = _compute_num_audio_tokens(self, "answer")
        return self.answer_ids.shape[0] + extra

    @property
    def total_length(self) -> int | None:
        if self.input_ids is None:
            return None
        extra = _compute_num_audio_tokens(self, "all")
        return self.input_ids.shape[0] + extra


def _compute_num_audio_tokens(example: NeMoMultimodalConversation, mode: Literal["context", "answer", "all"]) -> int:
    assert example.token_equivalent_duration is not None, (
        "Cannot compute the length of a NeMoMultimodalConversation: "
        "token_equivalent_duration must be set in order to estimate the number of tokens equivalent to audio turns. "
        "Did you forget to set token_equivalent_duration option in your dataloading config? "
        "Tip: generally it should be set to frame_shift * total_subsampling_factor of your audio encoder model."
    )
    match mode:
        case "context":
            turns = example.turns[:-1]
        case "answer":
            turns = example.turns[-1]
        case "all":
            turns = example.turns
        case _:
            raise RuntimeError(f"invalid mode for number of audio token computation: {mode}")
    return sum(
        [
            # subtract 1 for each audio locator tag as its token will be replaced
            math.ceil(turn.cut.duration / example.token_equivalent_duration) - 1
            for turn in turns
            if isinstance(turn, AudioTurn)
        ]
    )


@registered_prompt_format_fn(NeMoMultimodalConversation)
def default_multimodal_conversation_prompt_format_fn(example: NeMoMultimodalConversation, prompt):
    # Collapse consecutive same-role turns into single turn for proper prompt formatting.
    turns = groupby(
        [
            {
                "role": turn.role,
                "slots": {"message": turn.value if isinstance(turn, TextTurn) else turn.audio_locator_tag},
            }
            for turn in example.turns
        ],
        key=lambda turn: turn["role"],
    )
    turns = [
        {"role": role, "slots": {"message": " ".join(t["slots"]["message"] for t in turn_grp)}}
        for role, turn_grp in turns
    ]
    return prompt.encode_dialog(turns)


@dataclass
class NeMoMultimodalConversationJsonlAdapter:
    """
    ``NeMoMultimodalConversationJsonlAdapter`` is used to read a NeMo multimodal conversation JSONL
    and yield objects of type ``NeMoMultimodalConversation`` that can be sampled with Lhotse.

    We expect the following schema (contained in a single line per example)::

        {
            "id": str,
            "conversations": [
                {
                    "value": str,  # text message or path to audio
                    "from": "User" | "Assistant",
                    "type": "text" | "audio",
                    "duration": float,  # only for audio
                },
                ...
            ],
        }
    """

    manifest_filepath: str | list[str]
    audio_locator_tag: str
    tarred_audio_filepaths: str | list[str] = None
    token_equivalent_duration: float = None
    shuffle_shards: bool = False
    shard_seed: Union[int, Literal["trng", "randomized"]] = "trng"

    def __post_init__(self):
        self.manifest_filepath = expand_sharded_filepaths(self.manifest_filepath)
        if self.tarred_audio_filepaths is not None:
            raise NotImplementedError(
                "Tarred manifests are currently not supported yet for NeMoMultimodalConversation."
            )
            self.tarred_audio_filepaths = expand_sharded_filepaths(self.tarred_audio_filepaths)

    def __iter__(self) -> Iterator[NeMoMultimodalConversation]:
        paths = self.manifest_filepath
        if self.shuffle_shards:
            seed = resolve_seed(self.shard_seed)
            random.Random(seed).shuffle(paths)
        for path in paths:
            for data in load_jsonl(path):
                yield NeMoMultimodalConversation(
                    id=data["id"],
                    turns=[
                        (
                            TextTurn(
                                value=turn["value"],
                                role=turn[
                                    "from"
                                ].lower(),  # prompt formatter role's are typically lowercase: user/assistant
                            )
                            if turn["type"] == "text"
                            else AudioTurn(
                                cut=Recording.from_file(get_full_path(turn["value"], path)).to_cut(),
                                role=turn[
                                    "from"
                                ].lower(),  # prompt formatter role's are typically lowercase: user/assistant
                                audio_locator_tag=self.audio_locator_tag,
                            )
                        )
                        for turn in data["conversations"]
                    ],
                    token_equivalent_duration=self.token_equivalent_duration,
                )
