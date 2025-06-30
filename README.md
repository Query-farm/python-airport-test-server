# query-farm-airport-test-server

**`query-farm-airport-test-server`** is a Python module that implements a lightweight in-memory Arrow Flight server for use with the [Airport DuckDB extension](https://airport.query.farm). It showcases nearly all of the Airport extension's capabilities and is designed primarily for testing and CI integration.

> ⚠️ This server is not intended as a tutorial or reference for writing Arrow Flight servers from scratch. Its purpose is to comprehensively test feature coverage, and the implementation reflects that complexity.

## Features

- In-memory storage — no persistent state
- Accepts any authentication token
- Supports full reset of data via client call
- Ideal for CI pipelines and integration tests

## Installation

```sh
pip install query-farm-airport-test-server
```

## Usage

```sh
$ airport_test_server
```

Once running, the server can be used with the test suite included in the Airport DuckDB extension.