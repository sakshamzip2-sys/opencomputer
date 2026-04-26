from opencomputer.profile_bootstrap.identity_reflex import (
    IdentityFacts,
    _read_git_config_emails,
    _read_macos_contacts_me_name,
    gather_identity,
)
from unittest.mock import patch


def test_identity_facts_defaults():
    f = IdentityFacts()
    assert f.name == ""
    assert f.emails == ()
    assert f.github_handle is None


def test_identity_facts_immutable():
    import pytest
    f = IdentityFacts(name="Saksham")
    with pytest.raises(AttributeError):
        f.name = "Other"


def test_identity_facts_with_emails():
    f = IdentityFacts(name="Saksham", emails=("a@b.com", "c@d.com"))
    assert "a@b.com" in f.emails


def test_read_git_config_emails_returns_email():
    fake_output = "user.email=saksham@example.com\nuser.name=Saksham\n"
    with patch("subprocess.run") as mock:
        mock.return_value.stdout = fake_output
        mock.return_value.returncode = 0
        emails = _read_git_config_emails()
    assert "saksham@example.com" in emails


def test_read_git_config_emails_handles_missing_git():
    with patch("shutil.which", return_value=None):
        emails = _read_git_config_emails()
    assert emails == ()


def test_read_macos_contacts_returns_name():
    with patch("subprocess.run") as mock:
        mock.return_value.stdout = "Saksham\n"
        mock.return_value.returncode = 0
        name = _read_macos_contacts_me_name()
    assert name == "Saksham"


def test_read_macos_contacts_returns_none_on_failure():
    with patch("subprocess.run") as mock:
        mock.return_value.returncode = 1
        mock.return_value.stdout = ""
        name = _read_macos_contacts_me_name()
    assert name is None


def test_gather_identity_combines_sources():
    with patch(
        "opencomputer.profile_bootstrap.identity_reflex._read_git_config_emails",
        return_value=("a@b.com",),
    ), patch(
        "opencomputer.profile_bootstrap.identity_reflex._read_macos_contacts_me_name",
        return_value="Saksham",
    ):
        facts = gather_identity()
    assert facts.name == "Saksham"
    assert "a@b.com" in facts.emails
    assert facts.hostname  # set from socket.gethostname()
