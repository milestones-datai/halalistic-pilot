"""Unit tests for password hashing (argon2id)."""
from app.core.security import hash_password, verify_password


def test_hash_is_not_plaintext():
    pw = "hunter2-but-better"
    h = hash_password(pw)
    assert h != pw
    assert len(h) > 50  # argon2 hashes are long


def test_hash_is_unique_per_call_salt_is_random():
    pw = "same-password"
    a, b = hash_password(pw), hash_password(pw)
    assert a != b  # salt is random, so two hashes of the same password differ


def test_verify_accepts_correct_password():
    h = hash_password("correct-horse")
    assert verify_password("correct-horse", h) is True


def test_verify_rejects_wrong_password():
    h = hash_password("correct-horse")
    assert verify_password("wrong-horse", h) is False


def test_verify_handles_garbage_hash_safely():
    assert verify_password("any", "not-a-real-hash") is False
