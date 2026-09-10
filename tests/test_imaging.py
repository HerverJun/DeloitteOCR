from pathlib import Path
import tempfile
import unittest
from PIL import Image
from ocr_workbench.store import Store
from ocr_workbench.imaging import add_image, transform, digest, save_version


class ImagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")
        self.project = self.store.project("图片版本")

    def tearDown(self):
        self.temp.cleanup()

    def add(self, image, suffix=".png", **kwargs):
        path = self.root / ("图像" + suffix)
        image.save(path, **kwargs)
        photo = add_image(self.store, self.project["id"], path.name, path)
        return photo, self.store.one("versions", photo["active_version"])

    def test_exif_original_hash_and_immutable_processing_chain(self):
        original = Image.new("RGB", (80, 50), "white")
        exif = original.getexif()
        exif[274] = 6
        photo, base = self.add(original, ".jpg", exif=exif)
        self.assertEqual((base["width"], base["height"]), (50, 80))
        before = digest(self.store.file(photo["original_path"]))
        rotated = transform(self.store, base["id"], {"kind": "rotate", "degrees": 90})
        crop = transform(
            self.store, rotated["id"], {"kind": "crop", "box": [10, 5, 60, 35]}
        )
        self.assertEqual((crop["width"], crop["height"]), (50, 30))
        self.assertEqual(crop["parent_id"], rotated["id"])
        self.assertEqual(before, digest(self.store.file(photo["original_path"])))
        self.assertEqual(
            self.store.one("versions", base["id"])["sha256"], base["sha256"]
        )

    def test_transparency_flattens_white_and_perspective_preserves_colors(self):
        image = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
        image.paste((255, 0, 0, 255), (10, 10, 90, 70))
        _, base = self.add(image)
        with Image.open(self.store.file(base["path"])) as normalized:
            self.assertEqual(normalized.getpixel((0, 0)), (255, 255, 255))
        corrected = transform(
            self.store,
            base["id"],
            {"kind": "perspective", "points": [[10, 10], [89, 10], [89, 69], [10, 69]]},
        )
        with Image.open(self.store.file(corrected["path"])) as output:
            self.assertEqual(output.getpixel((20, 20)), (255, 0, 0))
        with self.assertRaises(ValueError):
            transform(
                self.store,
                base["id"],
                {
                    "kind": "perspective",
                    "points": [[0, 0], [100, 80], [100, 0], [0, 80]],
                },
            )

    def test_invalid_images_and_crops_leave_no_new_version(self):
        path = self.root / "坏图.png"
        path.write_bytes(b"not an image")
        with self.assertRaises(Exception):
            add_image(self.store, self.project["id"], path.name, path)
        self.assertEqual(self.store.rows("SELECT * FROM images"), [])
        _, base = self.add(Image.new("RGB", (100, 80), "white"))
        for box in [[-1, 0, 50, 50], [0, 0, 2, 2], [0, 0, 200, 80]]:
            with self.assertRaises(ValueError):
                transform(self.store, base["id"], {"kind": "crop", "box": box})
        self.assertEqual(len(self.store.rows("SELECT * FROM versions")), 1)

    def test_cancelled_dewarp_does_not_activate_a_late_version(self):
        photo, base = self.add(Image.new("RGB", (100, 80), "white"))
        key = self.store.enqueue_dewarp(base["id"])
        self.store.claim()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (key,))
        self.assertIsNone(
            save_version(
                self.store,
                base,
                Image.new("RGB", (70, 60)),
                {"kind": "dewarp"},
                task_id=key,
            )
        )
        self.assertEqual(
            self.store.one("images", photo["id"])["active_version"], base["id"]
        )

    def test_background_dewarp_does_not_override_newer_user_version(self):
        photo, base = self.add(Image.new("RGB", (100, 80), "white"))
        key = self.store.enqueue_dewarp(base["id"])
        self.store.claim()
        newer = transform(self.store, base["id"], {"kind": "rotate", "degrees": 90})
        warped = save_version(
            self.store,
            base,
            Image.new("RGB", (70, 60)),
            {"kind": "dewarp"},
            task_id=key,
        )
        self.assertEqual(
            self.store.one("images", photo["id"])["active_version"], newer["id"]
        )
        self.assertEqual(
            self.store.one("tasks", key)["result_version_id"], warped["id"]
        )
