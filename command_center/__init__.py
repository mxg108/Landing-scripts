"""Landing Ops Command Center — top-level package, mounted into the
AI-Scoring FastAPI app (qa-automation/AI-Scoring/backend/main.py).

See qa-automation/AI-Scoring/references/LandingOpsCommandCenter.md for the
project design and references/DispositionDesign.md for the v1 ingestion
base (webhook receiver + fold) implemented here.

NOTE: the design docs name this directory `command-center/`; it is spelled
`command_center/` because a hyphen is not importable as a Python package
name and the mount pattern (`from command_center.routes import ...`)
requires one.
"""
