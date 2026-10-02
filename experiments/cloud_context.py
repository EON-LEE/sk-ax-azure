"""Explicit, opt-in configuration for historical Azure experiment scripts."""
import os
import shutil


def legacy_cloud_context():
    if os.environ.get("AXK2_ENABLE_LEGACY_CLOUD") != "1":
        raise RuntimeError(
            "Historical cloud scripts are disabled. Review their fixed deadlines, "
            "resource ownership checks and costs before setting AXK2_ENABLE_LEGACY_CLOUD=1. "
            "This flag is not approval to incur costs or delete resources."
        )
    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    if not subscription or subscription == "00000000-0000-0000-0000-000000000000":
        raise RuntimeError("Set AZURE_SUBSCRIPTION_ID explicitly; no account is selected automatically.")
    cli = os.environ.get("AZURE_CLI") or shutil.which("az")
    if not cli:
        raise RuntimeError("Azure CLI not found. Install az or set AZURE_CLI to its executable.")
    return cli, subscription
