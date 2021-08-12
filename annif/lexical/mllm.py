"""MLLM (Maui-like Lexical Matchin) model for Annif"""

import collections
import math
import joblib
from statistics import mean
from enum import IntEnum
import numpy as np
from rdflib.namespace import SKOS
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.ensemble import BaggingClassifier
from sklearn.tree import DecisionTreeClassifier
import annif.util
import annif.parallel
from annif.lexical.tokenset import TokenSet, TokenSetIndex
from annif.lexical.util import get_subject_labels
from annif.lexical.util import make_relation_matrix, make_collection_matrix


Term = collections.namedtuple('Term', 'subject_id label is_pref')

Match = collections.namedtuple(
    'Match', 'subject_id is_pref n_tokens pos ambiguity')

Candidate = collections.namedtuple(
    'Candidate',
    'doc_length subject_id freq is_pref n_tokens ambiguity ' +
    'first_occ last_occ spread')

Feature = IntEnum(
    'Feature',
    'freq doc_freq subj_freq tfidf is_pref n_tokens ambiguity ' +
    'first_occ last_occ spread doc_length ' +
    'broader narrower related collection',
    start=0)


def conflate_matches(matches, doc_length):
    subj_matches = collections.defaultdict(list)
    for match in matches:
        subj_matches[match.subject_id].append(match)
    return [
        Candidate(
            doc_length=doc_length,
            subject_id=subject_id,
            freq=len(matches) / doc_length,
            is_pref=mean((float(m.is_pref) for m in matches)),
            n_tokens=mean((m.n_tokens for m in matches)),
            ambiguity=mean((m.ambiguity for m in matches)),
            first_occ=matches[0].pos / doc_length,
            last_occ=matches[-1].pos / doc_length,
            spread=(matches[-1].pos - matches[0].pos) / doc_length
        )
        for subject_id, matches in subj_matches.items()]


def generate_candidates(text, analyzer, vectorizer, index):
    sentences = analyzer.tokenize_sentences(text)
    sent_tokens = vectorizer.transform(sentences)
    matches = []

    for sent_idx, token_matrix in enumerate(sent_tokens):
        tset = TokenSet(token_matrix.nonzero()[1])
        for ts, ambiguity in index.search(tset):
            matches.append(Match(subject_id=ts.subject_id,
                                 is_pref=ts.is_pref,
                                 n_tokens=len(ts),
                                 pos=sent_idx,
                                 ambiguity=ambiguity))

    return conflate_matches(matches, len(sentences))


def candidates_to_features(candidates,
                           related_matrix, broader_matrix,
                           narrower_matrix, collection_matrix,
                           doc_freq, subj_freq, idf):
    """Convert a list of Candidates to a NumPy feature matrix"""

    matrix = np.zeros((len(candidates), len(Feature)), dtype=np.float32)
    c_ids = [c.subject_id for c in candidates]
    c_vec = np.zeros(related_matrix.shape[0], dtype=np.bool)
    c_vec[c_ids] = True
    broader = broader_matrix.multiply(c_vec).sum(axis=1)
    narrower = narrower_matrix.multiply(c_vec).sum(axis=1)
    related = related_matrix.multiply(c_vec).sum(axis=1)
    collection = collection_matrix.multiply(c_vec).T.dot(
        collection_matrix).sum(axis=0)
    for idx, c in enumerate(candidates):
        subj = c.subject_id
        matrix[idx, Feature.freq] = c.freq
        matrix[idx, Feature.doc_freq] = doc_freq[subj]
        matrix[idx, Feature.subj_freq] = subj_freq.get(subj, 1) - 1
        matrix[idx, Feature.tfidf] = c.freq * idf[subj]
        matrix[idx, Feature.is_pref] = c.is_pref
        matrix[idx, Feature.n_tokens] = c.n_tokens
        matrix[idx, Feature.ambiguity] = c.ambiguity
        matrix[idx, Feature.first_occ] = c.first_occ
        matrix[idx, Feature.last_occ] = c.last_occ
        matrix[idx, Feature.spread] = c.spread
        matrix[idx, Feature.doc_length] = c.doc_length
        matrix[idx, Feature.broader] = broader[subj, 0] / len(c_ids)
        matrix[idx, Feature.narrower] = narrower[subj, 0] / len(c_ids)
        matrix[idx, Feature.related] = related[subj, 0] / len(c_ids)
        matrix[idx, Feature.collection] = collection[0, subj] / len(c_ids)
    return matrix


class BaseWorker:
    args = None

    @classmethod
    def init(cls, args):
        cls.args = args


class MLLMCandidateGenerator(BaseWorker):

    @classmethod
    def generate_candidates(cls, doc_subject_ids, text):
        args = cls.args
        candidates = generate_candidates(
            text, args['analyzer'], args['vectorizer'], args['index'])
        return doc_subject_ids, candidates


class MLLMFeatureConverter(BaseWorker):

    @classmethod
    def candidates_to_features(cls, candidates):
        return candidates_to_features(candidates, **cls.args)


class MLLMModel:
    """Maui-like Lexical Matching model"""

    def generate_candidates(self, text, analyzer):
        return generate_candidates(text, analyzer,
                                   self._vectorizer, self._index)

    def _candidates_to_features(self, candidates):
        return candidates_to_features(candidates,
                                      self._related_matrix,
                                      self._broader_matrix,
                                      self._narrower_matrix,
                                      self._collection_matrix,
                                      self._doc_freq,
                                      self._subj_freq,
                                      self._idf)

    def _prepare_terms(self, graph, vocab, params):
        if annif.util.boolean(params['use_hidden_labels']):
            label_props = [SKOS.altLabel, SKOS.hiddenLabel]
        else:
            label_props = [SKOS.altLabel]

        terms = []
        subject_ids = []
        for subj_id, uri, pref, _ in vocab.subjects.active:
            subject_ids.append(subj_id)
            terms.append(Term(subject_id=subj_id, label=pref, is_pref=True))

            for label in get_subject_labels(graph, uri, label_props,
                                            params['language']):
                terms.append(Term(subject_id=subj_id,
                                  label=label,
                                  is_pref=False))

        return (terms, subject_ids)

    def _prepare_relations(self, graph, vocab):
        self._broader_matrix = make_relation_matrix(
            graph, vocab, SKOS.broader)
        self._narrower_matrix = make_relation_matrix(
            graph, vocab, SKOS.narrower)
        self._related_matrix = make_relation_matrix(
            graph, vocab, SKOS.related)
        self._collection_matrix = make_collection_matrix(graph, vocab)

    def _prepare_train_index(self, vocab, analyzer, params):
        graph = vocab.as_graph()
        terms, subject_ids = self._prepare_terms(graph, vocab, params)
        self._prepare_relations(graph, vocab)

        self._vectorizer = CountVectorizer(
            binary=True,
            tokenizer=analyzer.tokenize_words
        )
        label_corpus = self._vectorizer.fit_transform((t.label for t in terms))

        self._index = TokenSetIndex()
        for term, label_matrix in zip(terms, label_corpus):
            tokens = label_matrix.nonzero()[1]
            tset = TokenSet(tokens, term.subject_id, term.is_pref)
            self._index.add(tset)

        return subject_ids

    def _calculate_idf(self, subject_ids, doc_count):
        idf = collections.defaultdict(float)
        for subj_id in subject_ids:
            idf[subj_id] = math.log((doc_count + 1) /
                                    (self._doc_freq[subj_id] + 1)) + 1

        return idf

    def prepare_train(self, corpus, vocab, analyzer, params):
        subject_ids = self._prepare_train_index(vocab, analyzer, params)

        # frequency of subjects (by id) in the generated candidates
        self._doc_freq = collections.Counter()
        # frequency of manually assigned subjects ("domain keyphraseness")
        self._subj_freq = collections.Counter()
        doc_count = 0
        train_x = []
        train_y = []

        jobs, pool_class = annif.parallel.get_pool(4)

        cg_args = {
            'analyzer': analyzer,
            'vectorizer': self._vectorizer,
            'index': self._index
        }

        with pool_class(jobs,
                        initializer=MLLMCandidateGenerator.init,
                        initargs=(cg_args,)) as pool:
            params = (([vocab.subjects.by_uri(uri) for uri in doc.uris],
                       doc.text)
                      for doc in corpus.documents)
            for doc_subject_ids, candidates in pool.starmap(
                    MLLMCandidateGenerator.generate_candidates, params, 20):

                self._doc_freq.update([c.subject_id for c in candidates])
                train_x.append(candidates)
                train_y += [(c.subject_id in doc_subject_ids)
                            for c in candidates]
                doc_count += 1

        # precalculate idf values for all candidate subjects
        self._idf = self._calculate_idf(subject_ids, doc_count)

        fc_args = {
            'related_matrix': self._related_matrix,
            'broader_matrix': self._broader_matrix,
            'narrower_matrix': self._narrower_matrix,
            'collection_matrix': self._collection_matrix,
            'doc_freq': self._doc_freq,
            'subj_freq': self._subj_freq,
            'idf': self._idf
        }

        with pool_class(jobs,
                        initializer=MLLMFeatureConverter.init,
                        initargs=(fc_args,)) as pool:
            features = pool.map(
                MLLMFeatureConverter.candidates_to_features, train_x, 20)
        return (np.vstack(features), np.array(train_y))

    def _create_classifier(self, params):
        return BaggingClassifier(
            DecisionTreeClassifier(
                min_samples_leaf=int(params['min_samples_leaf']),
                max_leaf_nodes=int(params['max_leaf_nodes'])
            ), max_samples=float(params['max_samples']))

    def train(self, train_x, train_y, params):
        # fit the model on the training corpus
        self._classifier = self._create_classifier(params)
        self._classifier.fit(train_x, train_y)

    def _prediction_to_list(self, scores, candidates):
        subj_scores = [(score[1], c.subject_id)
                       for score, c in zip(scores, candidates)]
        return sorted(subj_scores, reverse=True)

    def predict(self, candidates):
        if not candidates:
            return []
        features = self._candidates_to_features(candidates)
        scores = self._classifier.predict_proba(features)
        return self._prediction_to_list(scores, candidates)

    def save(self, filename):
        return joblib.dump(self, filename)

    @staticmethod
    def load(filename):
        return joblib.load(filename)
