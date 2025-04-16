# Release process

### 1. Add changelog

### 2. Update version number

- `docs/conf.py`
- `electrumx/__init__.py`

### 3. Tag

```
$ git tag -s $VERSION -m "$VERSION"
$ git push "$REMOTE_ORIGIN" tag "$VERSION"
```

### 4. Build sdist

see [`contrib/sdist/`](contrib/sdist)

```
$ ELECBUILD_COMMIT=HEAD ELECBUILD_NOCACHE=1 ./contrib/sdist/build.sh
```

### 5. Upload to PyPI

```
// $ python3 -m twine upload --repository testpypi dist/$DISTNAME
$ python3 -m twine upload dist/$DISTNAME
```

This will prompt for a username and password. For the username, use `__token__`.
For the password, use API token from https://pypi.org/manage/account/ , including the pypi- prefix.
