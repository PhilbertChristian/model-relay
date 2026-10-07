# Weekend side projects

## greet-cli
test: python3 greet.py | grep -q Hello
notes: tiny Python CLI, standard library only
priority: 1

- [ ] scaffold greet.py that prints a greeting
- [ ] add a --name flag
- [ ] publish to PyPI

## landing-page
test: grep -q "<h1" index.html
notes: static HTML, no build step
priority: 2

- [ ] create index.html with a hero section
- [ ] add a pricing section

## api-stub
test: python3 -c "import api; assert api.health() == 'ok'"
priority: 3

- [ ] create api.py with a health() function
