"""
BMAD Automate - Automated BMAD Workflow Orchestrator.

A CLI tool that automates the BMAD (Business Method for Agile Development)
workflow cycle for stories defined in sprint-status.yaml.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bmad-automate")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"
