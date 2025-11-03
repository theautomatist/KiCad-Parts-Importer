import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from easyeda2kicad.api.server import create_app
from easyeda2kicad.service import ConversionRequest, ConversionResult, ConversionStage


def _dummy_runner(
    request: ConversionRequest, progress_cb
) -> ConversionResult:  # pragma: no cover - exercised through API
    result = ConversionResult(symbol_path=str(Path(request.output_prefix).resolve()))
    if progress_cb:
        progress_cb(ConversionStage.FETCHING, 50, "Fetching")
        progress_cb(ConversionStage.COMPLETED, 100, "Done")
    result.messages.append("ok")
    return result


class TaskApiTest(unittest.TestCase):
    def test_enqueue_and_complete(self) -> None:
        app = create_app(conversion_runner=_dummy_runner)
        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "lcsc_id": "C1234",
                    "output_path": "./tmp/testlib",
                    "symbol": True,
                },
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["id"]

            detail = None
            for _ in range(20):
                time.sleep(0.05)
                detail = client.get(f"/tasks/{task_id}")
                if detail.json()["status"] == "completed":
                    break

            self.assertIsNotNone(detail)
            self.assertEqual(detail.json()["status"], "completed")
            expected_path = str(Path("./tmp/testlib").resolve())
            self.assertEqual(detail.json()["result"]["symbol_path"], expected_path)

    def test_filesystem_helpers(self) -> None:
        app = create_app(conversion_runner=_dummy_runner)
        with TestClient(app) as client:
            roots = client.get("/fs/roots")
            self.assertEqual(roots.status_code, 200)
            data = roots.json()
            self.assertIsInstance(data, list)
            self.assertGreater(len(data), 0)

            first_root = data[0]["path"]
            listing = client.get("/fs/list", params={"path": first_root})
            self.assertEqual(listing.status_code, 200)
            listing_data = listing.json()
            self.assertEqual(listing_data["path"], str(Path(first_root).resolve()))

            check = client.post("/fs/check", json={"path": first_root})
            self.assertEqual(check.status_code, 200)
            check_data = check.json()
            self.assertTrue(check_data["resolved"])

    def test_overwrite_model_forwarded(self) -> None:
        captured = {}

        def runner(request: ConversionRequest, progress_cb) -> ConversionResult:
            captured["overwrite_model"] = request.overwrite_model
            if progress_cb:
                progress_cb(ConversionStage.FETCHING, 50, "Fetching")
                progress_cb(ConversionStage.COMPLETED, 100, "Done")
            result = ConversionResult(symbol_path=str(Path("./tmp/testlib").resolve()))
            result.messages.append("ok")
            return result

        app = create_app(conversion_runner=runner)
        with TestClient(app) as client:
            response = client.post(
                "/tasks",
                json={
                    "lcsc_id": "C5678",
                    "output_path": "./tmp/testlib",
                    "symbol": True,
                    "model": True,
                    "overwrite_model": True,
                },
            )
            self.assertEqual(response.status_code, 202)
            task_id = response.json()["id"]
            for _ in range(20):
                time.sleep(0.05)
                detail = client.get(f"/tasks/{task_id}")
                if detail.json()["status"] == "completed":
                    break
            self.assertTrue(captured.get("overwrite_model"))

    def test_library_scaffold_and_validate(self) -> None:
        app = create_app(conversion_runner=_dummy_runner)
        with tempfile.TemporaryDirectory() as tmpdir, TestClient(app) as client:
            payload = {
                "base_path": tmpdir,
                "library_name": "TestLib",
                "symbol": True,
                "footprint": True,
                "model": True,
            }
            response = client.post("/libraries/scaffold", json=payload)
            self.assertEqual(response.status_code, 201)
            data = response.json()
            self.assertTrue(Path(data["symbol_path"]).is_file())
            self.assertTrue(Path(data["footprint_dir"]).is_dir())
            self.assertTrue(Path(data["model_dir"]).is_dir())

            validate = client.post(
                "/libraries/validate", json={"path": data["resolved_library_prefix"]}
            )
            self.assertEqual(validate.status_code, 200)
            validation = validate.json()
            self.assertTrue(validation["exists"])
            self.assertTrue(validation["is_dir"])
            self.assertTrue(validation["assets"]["symbol"])
            self.assertTrue(validation["assets"]["footprint"])
            self.assertTrue(validation["assets"]["model"])
            self.assertGreaterEqual(validation["counts"].get("symbol"), 0)
            self.assertIsInstance(validation["counts"].get("footprint"), int)
            self.assertIsInstance(validation["counts"].get("model"), int)

            validate_file = client.post(
                "/libraries/validate", json={"path": data["symbol_path"]}
            )
            self.assertEqual(validate_file.status_code, 200)
            validation_file = validate_file.json()
            self.assertEqual(Path(validation_file["resolved_path"]).resolve(), Path(data["symbol_path"]).resolve())
            self.assertTrue(validation_file["exists"])
            self.assertFalse(validation_file["is_dir"])
            self.assertTrue(validation_file["assets"]["symbol"])
            self.assertGreaterEqual(validation_file["counts"].get("symbol"), 0)

    def test_symbol_counts_multiple_entries(self) -> None:
        app = create_app(conversion_runner=_dummy_runner)
        with tempfile.TemporaryDirectory() as tmpdir, TestClient(app) as client:
            sym_path = Path(tmpdir) / "multi.kicad_sym"
            sym_path.write_text(
                """
(kicad_symbol_lib (version 20211014) (generator test)
  (symbol "Device:R" (property "Reference" "R" (id 0)) )
  (symbol "Device:C" (property "Reference" "C" (id 0)) )
)
""".strip(),
                encoding="utf-8",
            )

            response = client.post("/libraries/validate", json={"path": str(sym_path)})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["assets"]["symbol"])
            self.assertEqual(data["counts"].get("symbol"), 2)
