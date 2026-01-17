import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from backup.models import Account, Provider
from backup.storage import (
    AccountStorage,
    BlobNotFoundError,
    DigestError,
    compute_digest,
    parse_digest,
)


class ParseDigestTests(TestCase):
    def test_valid_digest(self):
        digest = "sha256:" + "a" * 64
        algo, hex_value = parse_digest(digest)
        self.assertEqual(algo, "sha256")
        self.assertEqual(hex_value, "a" * 64)

    def test_invalid_format_no_colon(self):
        with self.assertRaises(ValueError) as ctx:
            parse_digest("sha256" + "a" * 64)
        self.assertIn("Invalid digest format", str(ctx.exception))

    def test_unsupported_algorithm(self):
        with self.assertRaises(ValueError) as ctx:
            parse_digest("md5:" + "a" * 32)
        self.assertIn("Unsupported digest algorithm", str(ctx.exception))

    def test_invalid_length(self):
        with self.assertRaises(ValueError) as ctx:
            parse_digest("sha256:" + "a" * 32)
        self.assertIn("Invalid digest length", str(ctx.exception))


class ComputeDigestTests(TestCase):
    def test_compute_from_bytes(self):
        data = b"hello world"
        digest = compute_digest(data)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(len(digest), 71)  # sha256: + 64 hex chars

    def test_compute_from_stream(self):
        data = b"hello world"
        stream = BytesIO(data)
        digest = compute_digest(stream)
        expected = compute_digest(data)
        self.assertEqual(digest, expected)

    def test_known_hash(self):
        # SHA256 of empty string
        digest = compute_digest(b"")
        self.assertEqual(
            digest,
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )


class AccountStorageTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.account = Account.objects.create(
            provider=Provider.GOOGLE_DRIVE,
            name="Test Account",
            email="test@example.com",
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def get_storage(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            return AccountStorage(self.account)

    @override_settings(BACKUP_ROOT=Path("/tmp/test_backup"))
    def test_init_paths(self):
        storage = AccountStorage(self.account)
        expected_root = (
            Path("/tmp/test_backup") / "google_drive" / str(self.account.id)
        )
        self.assertEqual(storage.root, expected_root)
        self.assertEqual(storage.current_dir, expected_root / "current")
        self.assertEqual(storage.blobs_dir, expected_root / "blobs")
        self.assertEqual(storage.tmp_dir, expected_root / "tmp")
        self.assertEqual(storage.archive_dir, expected_root / "archive")

    def test_ensure_directories(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            storage.ensure_directories()

            self.assertTrue(storage.current_dir.exists())
            self.assertTrue(storage.blobs_dir.exists())
            self.assertTrue(storage.tmp_dir.exists())
            self.assertTrue(storage.archive_dir.exists())

    def test_get_blob_path(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            digest = "sha256:abcdef1234567890" + "0" * 48

            path = storage.get_blob_path(digest)

            # Should be sharded: blobs/sha256/ab/cd/<full_hex>
            self.assertIn("sha256", str(path))
            self.assertIn("ab", str(path))
            self.assertIn("cd", str(path))
            self.assertTrue(str(path).endswith("abcdef1234567890" + "0" * 48))

    def test_write_and_read_blob_bytes(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"test content for blob storage"

            digest = storage.write_blob(data)

            self.assertTrue(digest.startswith("sha256:"))
            self.assertTrue(storage.blob_exists(digest))

            # Read it back
            content = storage.read_blob_bytes(digest)
            self.assertEqual(content, data)

    def test_write_blob_from_stream(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"streamed content"
            stream = BytesIO(data)

            digest = storage.write_blob(stream)

            content = storage.read_blob_bytes(digest)
            self.assertEqual(content, data)

    def test_write_blob_with_expected_digest(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"content with known hash"
            expected = compute_digest(data)

            digest = storage.write_blob(data, expected_digest=expected)

            self.assertEqual(digest, expected)

    def test_write_blob_with_wrong_expected_digest(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"some content"
            wrong_digest = "sha256:" + "0" * 64

            with self.assertRaises(DigestError):
                storage.write_blob(data, expected_digest=wrong_digest)

    def test_write_blob_deduplication(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"duplicate content"

            digest1 = storage.write_blob(data)
            digest2 = storage.write_blob(data)

            self.assertEqual(digest1, digest2)

    def test_read_nonexistent_blob(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            storage.ensure_directories()
            fake_digest = "sha256:" + "0" * 64

            with self.assertRaises(BlobNotFoundError):
                storage.read_blob(fake_digest)

    def test_read_blob_with_verification(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"content to verify"
            digest = storage.write_blob(data)

            # Reading with verification (default)
            with storage.read_blob(digest, verify=True) as f:
                content = f.read()
            self.assertEqual(content, data)

    def test_delete_blob(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"content to delete"
            digest = storage.write_blob(data)

            self.assertTrue(storage.blob_exists(digest))
            result = storage.delete_blob(digest)
            self.assertTrue(result)
            self.assertFalse(storage.blob_exists(digest))

    def test_delete_nonexistent_blob(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            storage.ensure_directories()
            fake_digest = "sha256:" + "0" * 64

            result = storage.delete_blob(fake_digest)
            self.assertFalse(result)

    def test_materialize_to_current(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"file content"
            digest = storage.write_blob(data)

            path = storage.materialize_to_current(digest, "folder/file.txt")

            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path, storage.current_dir / "folder" / "file.txt")

    def test_materialize_overwrites_existing(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            storage.ensure_directories()

            # Create initial file
            data1 = b"original content"
            digest1 = storage.write_blob(data1)
            storage.materialize_to_current(digest1, "file.txt")

            # Overwrite with new content
            data2 = b"new content"
            digest2 = storage.write_blob(data2)
            path = storage.materialize_to_current(digest2, "file.txt")

            self.assertEqual(path.read_bytes(), data2)

    def test_remove_from_current(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"content"
            digest = storage.write_blob(data)
            storage.materialize_to_current(digest, "folder/file.txt")

            result = storage.remove_from_current("folder/file.txt")

            self.assertTrue(result)
            self.assertFalse((storage.current_dir / "folder" / "file.txt").exists())

    def test_remove_nonexistent_from_current(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            storage.ensure_directories()

            result = storage.remove_from_current("nonexistent.txt")
            self.assertFalse(result)

    def test_move_to_archive(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"content to archive"
            digest = storage.write_blob(data)
            storage.materialize_to_current(digest, "folder/file.txt")

            archive_path = storage.move_to_archive("folder/file.txt")

            self.assertIsNotNone(archive_path)
            self.assertTrue(archive_path.exists())
            self.assertEqual(archive_path.read_bytes(), data)
            self.assertFalse((storage.current_dir / "folder" / "file.txt").exists())

    def test_restore_from_archive(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)
            data = b"archived content"
            digest = storage.write_blob(data)
            storage.materialize_to_current(digest, "file.txt")
            storage.move_to_archive("file.txt")

            restored_path = storage.restore_from_archive("file.txt")

            self.assertIsNotNone(restored_path)
            self.assertTrue(restored_path.exists())
            self.assertEqual(restored_path.read_bytes(), data)
            self.assertFalse((storage.archive_dir / "file.txt").exists())

    def test_get_storage_stats(self):
        with override_settings(BACKUP_ROOT=Path(self.temp_dir)):
            storage = AccountStorage(self.account)

            # Write some blobs and materialize
            data1 = b"content 1"
            data2 = b"content 2"
            digest1 = storage.write_blob(data1)
            digest2 = storage.write_blob(data2)
            storage.materialize_to_current(digest1, "file1.txt")
            storage.materialize_to_current(digest2, "file2.txt")

            stats = storage.get_storage_stats()

            self.assertEqual(stats["blob_count"], 2)
            self.assertEqual(stats["total_size_bytes"], len(data1) + len(data2))
            self.assertEqual(stats["current_file_count"], 2)
