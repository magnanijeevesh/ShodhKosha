"""Entry point used to build ShodhKosha.exe with PyInstaller.

For everyday development, run the application with:  python -m shodhkosha
"""

import sys

from shodhkosha.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
