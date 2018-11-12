"""PAV ensemble backend that combines results from multiple projects and
learns which concept suggestions from each backend are trustworthy using the
PAV algorithm, a.k.a. isotonic regression, to turn raw scores returned by
individual backends into probabilities."""

import os.path
from sklearn.externals import joblib
from sklearn.isotonic import IsotonicRegression
import numpy as np
import annif.corpus
import annif.hit
import annif.project
import annif.util
from annif.exception import NotInitializedException
from . import ensemble


class PAVBackend(ensemble.EnsembleBackend):
    """PAV ensemble backend that combines results from multiple projects"""
    name = "pav"

    MODEL_FILE_PREFIX = "pav-model-"

    # defaults for uninitialized instances
    _models = None

    def _get_model(self, source_project_id, project):
        if self._models is None:
            self._models = {}
        if source_project_id not in self._models:
            model_filename = self.MODEL_FILE_PREFIX + source_project_id
            path = os.path.join(self._get_datadir(), model_filename)
            if os.path.exists(path):
                self.debug('loading PAV model from {}'.format(path))
                self._models[source_project_id] = joblib.load(path)
            else:
                raise NotInitializedException(
                    "PAV model file '{}' not found".format(path),
                    project_id=project.project_id)
        return self._models[source_project_id]

    def _apply_pav(self, hits, source_project, project):
        reg_models = self._get_model(source_project.project_id, project)
        pav_result = []
        for hit in hits.hits:
            if hit.uri in reg_models:
                score = reg_models[hit.uri].predict([hit.score])[0]
            else:  # default to raw score
                score = hit.score
            pav_result.append(
                annif.hit.AnalysisHit(
                    uri=hit.uri,
                    label=hit.label,
                    score=score))
        pav_result.sort(key=lambda hit: hit.score, reverse=True)
        return annif.hit.ListAnalysisResult(
            pav_result, source_project.subjects)

    def _analyze_with_sources(self, text, sources, project):
        hits_from_sources = []
        for project_id, weight in sources:
            source_project = annif.project.get_project(project_id)
            hits = source_project.analyze(text)
            self.debug(
                'Got {} hits from project {}'.format(
                    len(hits), source_project.project_id))
            pav_hits = self._apply_pav(hits, source_project, project)
            self.debug(
                'Got {} hits after PAV'.format(len(pav_hits)))
            hits_from_sources.append(
                annif.hit.WeightedHits(
                    hits=pav_hits, weight=weight))
        return hits_from_sources

    def _analyze(self, text, project, params):
        sources = annif.util.parse_sources(params['sources'])
        hits_from_sources = self._analyze_with_sources(text, sources, project)
        merged_hits = annif.util.merge_hits(
            hits_from_sources, project.subjects)
        self.debug('{} hits after merging'.format(len(merged_hits)))
        return merged_hits

    @classmethod
    def _analyze_train_corpus(cls, source_project, corpus):
        scores = []
        true = []
        for doc in corpus.documents:
            hits = source_project.analyze(doc.text)
            scores.append(hits.vector)
            subjects = annif.corpus.SubjectSet((doc.uris, doc.labels))
            true.append(subjects.as_vector(source_project.subjects))
        return np.array(scores), np.array(true)

    def _create_pav_model(self, source_project_id, min_docs, corpus):
        self.info("creating PAV model for source {}, min_docs={}".format(
            source_project_id, min_docs))
        source_project = annif.project.get_project(source_project_id)
        # analyze the training corpus
        scores, true = self._analyze_train_corpus(source_project, corpus)
        # create the concept-specific PAV regression models
        pav_regressions = {}
        for cid in range(len(source_project.subjects)):
            if true[:, cid].sum() < min_docs:
                continue  # don't create model b/c of too few examples
            reg = IsotonicRegression(out_of_bounds='clip')
            reg.fit(scores[:, cid], true[:, cid])
            pav_regressions[source_project.subjects[cid][0]] = reg
        self.info("created PAV model for {} concepts".format(
            len(pav_regressions)))
        model_filename = self.MODEL_FILE_PREFIX + source_project_id
        annif.util.atomic_save(
            pav_regressions,
            self._get_datadir(),
            model_filename,
            method=joblib.dump)

    def load_corpus(self, corpus, project):
        self.info("creating PAV models")
        sources = annif.util.parse_sources(self.params['sources'])
        min_docs = int(self.params['min-docs'])
        for source_project_id, _ in sources:
            self._create_pav_model(source_project_id, min_docs, corpus)
