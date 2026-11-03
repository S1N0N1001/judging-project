import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from launcher import acquire_lock


@unittest.skipUnless(sys.platform == 'win32', 'Windows byte-range lock')
class LauncherTests(unittest.TestCase):
    def test_second_process_cannot_acquire_lock_then_can_after_release(self):
        with tempfile.TemporaryDirectory(dir='tmp') as folder:
            root=Path(folder).resolve()
            lock=acquire_lock(root)
            self.assertIsNotNone(lock)
            try:
                result=subprocess.run([sys.executable,'-c','from pathlib import Path; from launcher import acquire_lock; import sys; assert acquire_lock(Path(sys.argv[1])) is None',str(root)],timeout=10)
                self.assertEqual(result.returncode,0)
            finally:
                lock.close()
            second=acquire_lock(root)
            self.assertIsNotNone(second)
            second.close()
