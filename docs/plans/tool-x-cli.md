# Tool X: rename photos by date

## tool-x
test: python3 -m pytest -q
notes: Python 3.11, standard library only, a CLI called `toolx`

- [ ] scaffold toolx with a `rename` command
- [ ] read EXIF dates and rename files to YYYY-MM-DD_HHMM.jpg
- [ ] add a --dry-run flag that prints the plan without renaming
- [ ] write tests for photos without EXIF data
- [ ] add a README with install and usage examples
- [ ] publish to PyPI
