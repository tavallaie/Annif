"""Transformation filtering out parts of a text that are in a language
different from the language of the project."""
from __future__ import annotations

from typing import TYPE_CHECKING

from simplemma import LanguageDetector

import annif
import annif.langsupport

from . import transform

if TYPE_CHECKING:
    from annif.project import AnnifProject

logger = annif.logger


class LangFilter(transform.BaseTransform):
    name = "filter_lang"

    def __init__(
        self,
        project: AnnifProject,
        text_min_length: int | str = 500,
        sentence_min_length: int | str = 50,
        min_ratio: float = 0.5,
    ) -> None:
        super().__init__(project)
        self.text_min_length = int(text_min_length)
        self.sentence_min_length = int(sentence_min_length)
        self.min_ratio = float(min_ratio)
        self.language_detector = LanguageDetector(
            self.project.language,
            lemmatization_strategy=annif.langsupport.lemmatization_strategy,
        )

    def transform_fn(self, text: str) -> str:
        if len(text) < self.text_min_length:
            return text

        retained_sentences = []
        for sent in self.project.analyzer.tokenize_sentences(text):
            if len(sent) < self.sentence_min_length:
                retained_sentences.append(sent)
                continue
            proportion = self.language_detector.proportion_in_target_languages(sent)
            if proportion >= self.min_ratio:
                retained_sentences.append(sent)
        return " ".join(retained_sentences)
