"""Поиск кандидатов (R15): `scan()` по лентам, отпечаток, очередь и решения оператора."""

from lab.discovery.config import DiscoveryConfig, load_discovery
from lab.discovery.decisions import (
    CandidateNotFound,
    DecisionResult,
    candidate_hook,
    decide,
    manifest_for,
)
from lab.discovery.scan import Discovery, ScanResult, candidate_payload, scan
from lab.discovery.sources import (
    BaseSource,
    GithubSource,
    HyperliquidLeaderSource,
    NftLaunchpadSource,
    OkxLeadSource,
    PolymarketLeaderSource,
    SeedFileSource,
    SmartMoneySource,
    default_sources,
)
from lab.discovery.types import CandidateSource, CandidateSpec, bucket, fingerprint

__all__ = [
    "BaseSource",
    "CandidateNotFound",
    "CandidateSource",
    "CandidateSpec",
    "DecisionResult",
    "Discovery",
    "DiscoveryConfig",
    "GithubSource",
    "HyperliquidLeaderSource",
    "NftLaunchpadSource",
    "OkxLeadSource",
    "PolymarketLeaderSource",
    "ScanResult",
    "SeedFileSource",
    "SmartMoneySource",
    "bucket",
    "candidate_hook",
    "candidate_payload",
    "decide",
    "default_sources",
    "fingerprint",
    "load_discovery",
    "manifest_for",
    "scan",
]
