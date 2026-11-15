from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
AWS_OPS = ROOT / "ops" / "aws"


class AwsSystemdOperationTests(unittest.TestCase):
    def test_policy_service_chains_web_only_on_success(self) -> None:
        content = (
            AWS_OPS / "youthlink-policy-pipeline.service.template"
        ).read_text(encoding="utf-8")

        self.assertIn("OnSuccess=youthlink-web-pipeline.service", content)
        self.assertIn(
            "etl/run_policy_pipeline.py --resume-latest-failed", content
        )
        self.assertIn("Restart=on-failure", content)
        self.assertIn("EnvironmentFile=-@REPO_DIR@/.env", content)
        self.assertNotIn("YOUTH_API_KEY=", content)
        self.assertNotIn("LITELLM_API_KEY=", content)

    def test_web_service_runs_pipeline_then_health_check(self) -> None:
        content = (
            AWS_OPS / "youthlink-web-pipeline.service.template"
        ).read_text(encoding="utf-8")

        pipeline_position = content.index("etl/run_web_pipeline.py")
        health_position = content.index("etl/check_pipeline_health.py")
        self.assertLess(pipeline_position, health_position)
        self.assertIn("--resume-latest-failed", content)
        self.assertIn("RestartSec=15min", content)

    def test_timer_uses_kst_and_catches_missed_runs(self) -> None:
        content = (
            AWS_OPS / "youthlink-policy-pipeline.timer.template"
        ).read_text(encoding="utf-8")

        self.assertIn("OnCalendar=@ON_CALENDAR@", content)
        self.assertIn("Persistent=true", content)
        self.assertIn("RandomizedDelaySec=0", content)

    def test_installer_validates_runtime_and_keeps_secrets_out_of_units(self) -> None:
        content = (AWS_OPS / "install_systemd.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("--check-only", content)
        self.assertIn("chmod 600 .env", content)
        self.assertIn("systemd-analyze verify", content)
        self.assertIn("systemctl enable --now", content)
        self.assertIn("LITELLM_CHAT_MODEL", content)
        self.assertNotIn("YOUR_PASSWORD", content)

    def test_verifier_is_read_only_and_reports_both_services(self) -> None:
        content = (AWS_OPS / "verify_systemd.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("systemctl show", content)
        self.assertIn("systemctl is-enabled", content)
        self.assertIn("ActiveState", content)
        self.assertIn("ExecMainExitTimestamp", content)
        self.assertIn("etl/check_pipeline_health.py --json", content)
        self.assertIn("verification_result=OK", content)
        self.assertIn("youthlink-policy-pipeline.service", content)
        self.assertIn("youthlink-web-pipeline.service", content)
        self.assertNotIn("systemctl start", content)
        self.assertNotIn("run_policy_pipeline.py", content)
        self.assertNotIn("run_web_pipeline.py", content)


if __name__ == "__main__":
    unittest.main()
