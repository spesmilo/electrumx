# Source tarballs

✓ _These tarballs should be reproducible, meaning you should be able to generate
   distributables that match the official releases._

This assumes an Ubuntu (x86_64) host, but it should not be too hard to adapt to another
similar system.


## Build steps

1. Install Docker

    See [`docker_notes.md`](https://github.com/spesmilo/electrum/blob/master/contrib/docker_notes.md).

    (worth reading even if you already have docker)

2. Build tarball

    ```
    $ ./contrib/sdist/build.sh
    ```
    If you want reproducibility, try instead e.g.:
    ```
    $ ELECBUILD_COMMIT=HEAD ELECBUILD_NOCACHE=1 ./contrib/sdist/build.sh
    ```

3. The generated distributables are in `./dist`.

