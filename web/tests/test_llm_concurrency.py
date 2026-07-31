"""Tests for the partitioned LLM concurrency limiter (producer-consumer model)."""

import asyncio

import pytest

import llm_concurrency


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Reset the lazily-created semaphores between tests."""
    monkeypatch.setattr(llm_concurrency, "_GLOBAL_LLM_SEMAPHORE", None)
    monkeypatch.setattr(llm_concurrency, "_CONSUMER_LLM_SEMAPHORE", None)
    monkeypatch.setattr(llm_concurrency, "_PRODUCER_LLM_SEMAPHORE", None)
    yield


def test_partition_split_logic():
    """The split logic: consumer = max(1, total//3), producer = total - consumer."""
    # total=3 → consumer=1, producer=2
    assert max(1, 3 // 3) == 1
    assert max(1, 3 - 1) == 2
    # total=2 → consumer=1 (max(1, 0)), producer=1
    assert max(1, 2 // 3) == 1
    assert max(1, 2 - 1) == 1
    # total=6 → consumer=2, producer=4
    assert max(1, 6 // 3) == 2
    assert max(1, 6 - 2) == 4


def test_consumer_and_producer_semaphores_are_distinct():
    """The two sub-pools are separate Semaphore instances."""
    consumer = llm_concurrency.get_consumer_llm_semaphore()
    producer = llm_concurrency.get_producer_llm_semaphore()
    assert consumer is not producer
    assert consumer._value == llm_concurrency.CONSUMER_LLM_CONCURRENCY
    assert producer._value == llm_concurrency.PRODUCER_LLM_CONCURRENCY


def test_role_classification_review_is_consumer():
    """Review/critic roles route to the consumer sub-pool."""
    sem_review = llm_concurrency.get_llm_semaphore_for_role("review")
    sem_critic = llm_concurrency.get_llm_semaphore_for_role("critic")
    consumer = llm_concurrency.get_consumer_llm_semaphore()
    assert sem_review is consumer
    assert sem_critic is consumer


def test_role_classification_master_is_producer():
    """Master/Worker/direction roles route to the producer sub-pool."""
    sem_master = llm_concurrency.get_llm_semaphore_for_role("master")
    sem_worker = llm_concurrency.get_llm_semaphore_for_role("worker")
    sem_direction = llm_concurrency.get_llm_semaphore_for_role("direction_audit")
    producer = llm_concurrency.get_producer_llm_semaphore()
    assert sem_master is producer
    assert sem_worker is producer
    assert sem_direction is producer


def test_role_classification_none_is_producer():
    """Unknown/None role defaults to the producer lane."""
    sem = llm_concurrency.get_llm_semaphore_for_role(None)
    assert sem is llm_concurrency.get_producer_llm_semaphore()


def test_role_classification_substring_match():
    """Role names containing the marker (e.g. 'master_review') still classify."""
    # 'review' substring → consumer
    assert llm_concurrency.get_llm_semaphore_for_role("code_review") is llm_concurrency.get_consumer_llm_semaphore()
    # 'critic' substring → consumer
    assert llm_concurrency.get_llm_semaphore_for_role("advisory_critic_check") is llm_concurrency.get_consumer_llm_semaphore()
    # 'master' (no marker) → producer
    assert llm_concurrency.get_llm_semaphore_for_role("master_proposal_1") is llm_concurrency.get_producer_llm_semaphore()

