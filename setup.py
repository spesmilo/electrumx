import os
import re

import setuptools


def find_version():
    tld = os.path.abspath(os.path.dirname(__file__))
    filename = os.path.join(tld, 'electrumx', '__init__.py')
    with open(filename) as f:
        text = f.read()
    match = re.search(r"^__version__ = \"(.*)\"$", text, re.MULTILINE)
    if not match:
        raise RuntimeError('cannot find version')
    return match.group(1)


version = find_version()

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

setuptools.setup(
    name='e-x',
    version=version,
    scripts=['electrumx_server', 'electrumx_rpc', 'electrumx_compact_history'],
    python_requires='>=3.10',
    install_requires=requirements,
    extras_require={
        'dev': ['objgraph'],
        'rapidjson': ['python-rapidjson>=0.4.1,<2.0'],
        'rocksdb': ['python-rocksdb>=0.6.9', 'Cython<3.0'],
        'ujson': ['ujson>=2.0.0,<4.0.0'],
        'uvloop': ['uvloop>=0.14'],
        # For various coins
        'blake256': ['blake256>=0.1.1'],
        'crypto': ['pycryptodomex>=3.8.1'],
        'groestl': ['groestlcoin-hash>=1.0.1'],
        'tribushashm': ['tribushashm>=1.0.5'],
        'xevan-hash': ['xevan-hash'],
        'dash_hash': ['dash_hash>=1.4'],
        'zny-yespower-0-5': ['zny-yespower-0-5'],
    },
    packages=setuptools.find_packages(include=('electrumx*',)),
    description='ElectrumX Server',
    author='Electrum developers',
    author_email='electrumdev@gmail.com',
    license='MIT Licence',
    url='https://github.com/spesmilo/electrumx',
    long_description='Server implementation for the Electrum protocol',
    download_url=('https://github.com/spesmilo/electrumX/archive/'
                  f'{version}.tar.gz'),
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Framework :: AsyncIO',
        'License :: OSI Approved :: MIT License',
        'Operating System :: Unix',
        "Programming Language :: Python :: 3.10",
        "Topic :: Database",
        'Topic :: Internet',
    ],
)
