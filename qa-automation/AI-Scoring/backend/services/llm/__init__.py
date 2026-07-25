"""Text-model provider seam — ModelProviderDesign §3 / TwoStageScoringDesign §4.

Neutral contract in ``provider.py``; ``gemini.py`` and ``anthropic.py``
are the ONLY vendor-shaped modules (grep-gated); ``factory.py`` resolves
a stage (progression today, the Stage-B scorer next) to a configured
provider + model.
"""
