# config/

The config folder (`CONFIG_DIR`): `settings.json`, `models.json`, `rates.json` and `backups/`, the copies
taken before every save. `setup.sh` copies `src/config/settings.example.json` and `src/config/rates.example.json`
here; by hand, copy them to `settings.json` and `rates.json`.
Everything here except this file is ignored by git.
