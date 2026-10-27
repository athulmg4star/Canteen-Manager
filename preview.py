"""Preview with a disposable database; changes disappear when preview stops."""
import os
import sqlite3
import tempfile
from pathlib import Path
import app

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='tiffin-v3-preview-') as directory:
        source_path = Path(app.DB_FILE).resolve()
        preview_path = Path(directory) / 'preview.db'
        if source_path.exists():
            with sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(preview_path) as destination:
                source.backup(destination)
        app.DB_FILE = str(preview_path)
        app.init_db()
        app.app.config['TEMPLATES_AUTO_RELOAD'] = True
        app.app.config['PREVIEW_MODE'] = True
        print('PREVIEW ONLY: changes are saved to a temporary copy, never the office database.')
        app.app.run(host='127.0.0.1', port=int(os.environ.get('TIFFIN_PREVIEW_PORT', '5003')), debug=False, use_reloader=False)
