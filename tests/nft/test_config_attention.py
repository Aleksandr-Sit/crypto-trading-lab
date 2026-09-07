"""Конфиг ветки NFT и индекс внимания (Истории 74, 74a)."""

from decimal import Decimal

import pytest

from lab.feeds.nft import load_nft
from lab.nft import AttentionComponents, attention_index


def test_config_carries_attention_weights_and_marks_them_hypothesis():
    config = load_nft()
    weights = config.attention.weights
    assert weights.mentions_growth == Decimal("0.35")
    assert weights.creator_score == Decimal("0.20")
    assert config.attention.hypothesis is True, "веса — гипотеза, это должно быть видно в конфиге"
    assert sum(weights.as_dict().values()) == Decimal(1)


def test_config_marks_blur_read_only_and_tensor_disabled():
    config = load_nft()
    assert config.markets["blur"].read_only is True
    assert config.markets["tensor"].enabled is False
    assert config.markets["magiceden"].read_only is False


def test_attention_index_is_weighted_sum_of_components():
    config = load_nft()
    components = AttentionComponents(
        mentions_growth=Decimal(1),
        allowlist_demand=Decimal("0.5"),
        launchpad_fill=Decimal(0),
        creator_score=Decimal("0.25"),
    )
    # 0.35*1 + 0.25*0.5 + 0.20*0 + 0.20*0.25 = 0.35 + 0.125 + 0 + 0.05 = 0.525
    index = attention_index(components, config=config)
    assert index.value == Decimal("0.525")
    assert index.hypothesis is True
    assert index.components["mentions_growth"] == Decimal(1)
    assert index.weights["creator_score"] == Decimal("0.20")


def test_attention_components_clamped_to_unit_interval():
    config = load_nft()
    index = attention_index(
        AttentionComponents(mentions_growth=Decimal(5), creator_score=Decimal(-2)), config=config
    )
    assert index.components["mentions_growth"] == Decimal(1)
    assert index.components["creator_score"] == Decimal(0)
    assert index.value == Decimal("0.35")


def test_unknown_config_path_is_reported():
    from lab.config import ConfigError

    with pytest.raises(ConfigError):
        load_nft("config/does-not-exist.yaml")
