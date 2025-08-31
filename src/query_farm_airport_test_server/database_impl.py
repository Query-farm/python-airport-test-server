import datetime
import os
import pickle
import tempfile
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, overload

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.flight as flight
import query_farm_flight_server.flight_handling as flight_handling
import query_farm_flight_server.flight_inventory as flight_inventory
import query_farm_flight_server.parameter_types as parameter_types

from .utils import CaseInsensitiveDict

# Since we are creating a new database, lets load it with a few example
# scalar functions.


@dataclass
class TableFunctionDynamicOutput:
    # The method that will determine the output schema from the input parameters
    schema_creator: Callable[[pa.RecordBatch, pa.Schema | None], pa.Schema]

    # The default parameters for the function, if not called with any.
    default_values: tuple[pa.RecordBatch, pa.Schema | None]


@dataclass
class TableFunction:
    # The input schema for the function.
    input_schema: pa.Schema

    output_schema_source: pa.Schema | TableFunctionDynamicOutput

    # The function to call to process a chunk of rows.
    handler: Callable[[parameter_types.TableFunctionParameters, pa.Schema], parameter_types.TableFunctionInOutGenerator]

    estimated_rows: int | Callable[[parameter_types.TableFunctionFlightInfo], int] = -1

    def output_schema(
        self,
        parameters: pa.RecordBatch | None = None,
        input_schema: pa.Schema | None = None,
    ) -> pa.Schema:
        if isinstance(self.output_schema_source, pa.Schema):
            return self.output_schema_source
        if parameters is None:
            return self.output_schema_source.schema_creator(*self.output_schema_source.default_values)
        assert isinstance(parameters, pa.RecordBatch)
        result = self.output_schema_source.schema_creator(parameters, input_schema)
        return result

    def flight_info(
        self,
        *,
        name: str,
        catalog_name: str,
        schema_name: str,
        parameters: parameter_types.TableFunctionFlightInfo | None = None,
    ) -> tuple[flight.FlightInfo, flight_inventory.FlightSchemaMetadata]:
        """
        Often its necessary to create a FlightInfo object
        standardize doing that here.
        """
        assert name != ""
        assert catalog_name != ""
        assert schema_name != ""

        if isinstance(self.estimated_rows, int):
            estimated_rows = self.estimated_rows
        else:
            assert parameters is not None
            estimated_rows = self.estimated_rows(parameters)

        metadata = flight_inventory.FlightSchemaMetadata(
            type="table_function",
            catalog=catalog_name,
            schema=schema_name,
            name=name,
            comment=None,
            input_schema=self.input_schema,
        )
        flight_info = flight.FlightInfo(
            self.output_schema(parameters.parameters, parameters.table_input_schema)
            if parameters
            else self.output_schema(),
            # This will always be the same descriptor, so that we can use the action
            # name to determine which which table function to execute.
            descriptor_pack_(catalog_name, schema_name, "table_function", name),
            [],
            estimated_rows,
            -1,
            app_metadata=metadata.serialize(),
        )
        return (flight_info, metadata)


@dataclass
class ScalarFunction:
    # The input schema for the function.
    input_schema: pa.Schema
    # The output schema for the function, should only have a single column.
    output_schema: pa.Schema

    # The function to call to process a chunk of rows.
    handler: Callable[[pa.Table], pa.Array]

    stability: flight_inventory.ScalarFunctionStability

    def flight_info(
        self, *, name: str, catalog_name: str, schema_name: str
    ) -> tuple[flight.FlightInfo, flight_inventory.FlightSchemaMetadata]:
        """
        Often its necessary to create a FlightInfo object
        standardize doing that here.
        """
        metadata = flight_inventory.ScalarFunctionMetadata(
            catalog=catalog_name,
            schema=schema_name,
            name=name,
            comment=None,
            input_schema=self.input_schema,
            stability=self.stability,
        )
        flight_info = flight.FlightInfo(
            self.output_schema,
            descriptor_pack_(catalog_name, schema_name, "scalar_function", name),
            [],
            -1,
            -1,
            app_metadata=metadata.serialize(),
        )
        return (flight_info, metadata)


def serialize_table_data(table: pa.Table) -> bytes:
    """
    Serialize the table data to a byte string.
    """
    assert isinstance(table, pa.Table)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def deserialize_table_data(data: bytes) -> pa.Table:
    """
    Deserialize the table data from a byte string.
    """
    assert isinstance(data, bytes)
    buffer = pa.BufferReader(data)
    ipc_stream = pa.ipc.open_stream(buffer)
    return ipc_stream.read_all()


@dataclass
class TableInfo:
    # To enable version history keep track of tables.
    table_versions: list[pa.Table] = field(default_factory=list)

    primary_key_columns: list[str] = field(default_factory=list)
    unique_columns: list[str] = field(default_factory=list)
    multi_key_primary_keys: list[str] = field(default_factory=list)
    extra_constraints: list[str] = field(default_factory=list)

    # the next row id to assign.
    row_id_counter: int = 0

    # This cannot be serailized but it convenient for testing.
    endpoint_generator: Callable[[Any], list[flight.FlightEndpoint]] | None = None

    def update_table(self, table: pa.Table) -> None:
        assert table is not None
        assert isinstance(table, pa.Table)
        self.table_versions.append(table)

    def version(self, version: int | None = None) -> pa.Table:
        """
        Get the version of the table.
        """
        assert len(self.table_versions) > 0
        if version is None:
            return self.table_versions[-1]

        assert version < len(self.table_versions)
        return self.table_versions[version]

    def flight_info(
        self,
        *,
        name: str,
        catalog_name: str,
        schema_name: str,
        version: int | None = None,
    ) -> tuple[flight.FlightInfo, flight_inventory.FlightSchemaMetadata]:
        """
        Often its necessary to create a FlightInfo object for the table,
        standardize doing that here.
        """
        metadata = flight_inventory.FlightSchemaMetadata(
            type="table",
            catalog=catalog_name,
            schema=schema_name,
            name=name,
            comment=None,
        )
        flight_info = flight.FlightInfo(
            self.version(version).schema,
            descriptor_pack_(catalog_name, schema_name, "table", name),
            [],
            -1,
            -1,
            app_metadata=metadata.serialize(),
        )
        return (flight_info, metadata)

    def serialize(self) -> dict[str, Any]:
        """
        Serialize the TableInfo to a dictionary.
        """
        return {
            "table_versions": [serialize_table_data(table) for table in self.table_versions],
            "row_id_counter": self.row_id_counter,
            "primary_key_columns": self.primary_key_columns,
            "unique_columns": self.unique_columns,
            "multi_key_primary_keys": self.multi_key_primary_keys,
            "extra_constraints": self.extra_constraints,
        }

    def deserialize(self, data: dict[str, Any]) -> "TableInfo":
        """
        Deserialize the TableInfo from a dictionary.
        """
        self.table_versions = [deserialize_table_data(table) for table in data["table_versions"]]
        self.row_id_counter = data["row_id_counter"]
        self.endpoint_generator = None
        self.primary_key_columns = data.get("primary_key_columns", [])
        self.unique_columns = data.get("unique_columns", [])
        self.multi_key_primary_keys = data.get("multi_key_primary_keys", [])
        self.extra_constraints = data.get("extra_constraints", [])
        return self


ObjectTypeName = Literal["table", "scalar_function", "table_function"]


@dataclass
class DescriptorParts:
    """
    The fields that are encoded in the flight descriptor.
    """

    catalog_name: str
    schema_name: str
    type: ObjectTypeName
    name: str


def descriptor_pack_(
    catalog_name: str,
    schema_name: str,
    type: ObjectTypeName,
    name: str,
) -> flight.FlightDescriptor:
    """
    Pack the descriptor into a FlightDescriptor.
    """
    return flight.FlightDescriptor.for_path(f"{catalog_name}/{schema_name}/{type}/{name}")


def descriptor_unpack_(descriptor: flight.FlightDescriptor) -> DescriptorParts:
    """
    Split the descriptor into its components.
    """
    assert descriptor.descriptor_type == flight.DescriptorType.PATH
    assert len(descriptor.path) == 1
    path = descriptor.path[0].decode("utf-8")
    parts = path.split("/")
    if len(parts) != 4:
        raise flight.FlightServerError(f"Invalid descriptor path: {path}")

    descriptor_type: ObjectTypeName
    if parts[2] == "table":
        descriptor_type = "table"
    elif parts[2] == "scalar_function":
        descriptor_type = "scalar_function"
    elif parts[2] == "table_function":
        descriptor_type = "table_function"
    else:
        raise flight.FlightServerError(f"Invalid descriptor type: {parts[2]}")

    return DescriptorParts(
        catalog_name=parts[0],
        schema_name=parts[1],
        type=descriptor_type,
        name=parts[3],
    )


@dataclass
class SchemaCollection:
    tables_by_name: CaseInsensitiveDict[TableInfo] = field(default_factory=CaseInsensitiveDict[TableInfo])
    scalar_functions_by_name: CaseInsensitiveDict[ScalarFunction] = field(
        default_factory=CaseInsensitiveDict[ScalarFunction]
    )
    table_functions_by_name: CaseInsensitiveDict[TableFunction] = field(
        default_factory=CaseInsensitiveDict[TableFunction]
    )

    def serialize(self) -> dict[str, Any]:
        return {
            "tables": {name: table.serialize() for name, table in self.tables_by_name.items()},
        }

    def deserialize(self, data: dict[str, Any]) -> "SchemaCollection":
        """
        Deserialize the schema collection from a dictionary.
        """
        self.tables_by_name = CaseInsensitiveDict[TableInfo](
            {name: TableInfo().deserialize(table) for name, table in data["tables"].items()}
        )
        return self

    def containers(
        self,
    ) -> list[
        CaseInsensitiveDict[TableInfo] | CaseInsensitiveDict[ScalarFunction] | CaseInsensitiveDict[TableFunction]
    ]:
        return [
            self.tables_by_name,
            self.scalar_functions_by_name,
            self.table_functions_by_name,
        ]

    @overload
    def by_name(self, type: Literal["table"], name: str) -> TableInfo: ...

    @overload
    def by_name(self, type: Literal["scalar_function"], name: str) -> ScalarFunction: ...

    @overload
    def by_name(self, type: Literal["table_function"], name: str) -> TableFunction: ...

    def by_name(self, type: ObjectTypeName, name: str) -> TableInfo | ScalarFunction | TableFunction:
        assert name is not None
        assert name != ""
        if type == "table":
            table = self.tables_by_name.get(name)
            if not table:
                raise flight.FlightServerError(f"Table {name} does not exist.")
            return table
        elif type == "scalar_function":
            scalar_function = self.scalar_functions_by_name.get(name)
            if not scalar_function:
                raise flight.FlightServerError(f"Scalar function {name} does not exist.")
            return scalar_function
        elif type == "table_function":
            table_function = self.table_functions_by_name.get(name)
            if not table_function:
                raise flight.FlightServerError(f"Table function {name} does not exist.")
            return table_function


@dataclass
class DatabaseContents:
    # Collection of schemas by name.
    schemas_by_name: CaseInsensitiveDict[SchemaCollection] = field(
        default_factory=CaseInsensitiveDict[SchemaCollection]
    )

    # The version of the database, updated on each schema change.
    version: int = 1

    def __post_init__(self) -> None:
        self.schemas_by_name["remote_data"] = remote_data_schema
        self.schemas_by_name["static_data"] = static_data_schema
        self.schemas_by_name["utils"] = util_schema
        return

    def by_name(self, name: str) -> SchemaCollection:
        if name not in self.schemas_by_name:
            raise flight.FlightServerError(f"Schema {name} does not exist.")
        return self.schemas_by_name[name]

    def serialize(self) -> dict[str, Any]:
        return {
            "schemas": {
                name: schema.serialize()
                for name, schema in self.schemas_by_name.items()
                if name not in ("static_data", "remote_data", "utils")
            },
            "version": self.version,
        }

    def deserialize(self, data: dict[str, Any]) -> "DatabaseContents":
        """
        Deserialize the database contents from a dictionary.
        """
        self.schemas_by_name = CaseInsensitiveDict[SchemaCollection](
            {name: SchemaCollection().deserialize(schema) for name, schema in data["schemas"].items()}
        )
        self.schemas_by_name["static_data"] = static_data_schema
        self.schemas_by_name["remote_data"] = remote_data_schema
        self.schemas_by_name["utils"] = util_schema

        self.version = data["version"]
        return self


@dataclass
class DatabaseLibrary:
    """
    The database library, which contains all of the databases, organized by token.
    """

    # Collection of databases by token.
    databases_by_name: CaseInsensitiveDict[DatabaseContents] = field(
        default_factory=CaseInsensitiveDict[DatabaseContents]
    )

    def by_name(self, name: str) -> DatabaseContents:
        if name not in self.databases_by_name:
            raise flight.FlightServerError(f"Database {name} does not exist.")
        return self.databases_by_name[name]

    def serialize(self) -> dict[str, Any]:
        return {
            "databases": {name: db.serialize() for name, db in self.databases_by_name.items()},
        }

    def deserialize(self, data: dict[str, Any]) -> None:
        """
        Deserialize the database library from a dictionary.
        """
        self.databases_by_name = CaseInsensitiveDict[DatabaseContents](
            {name: DatabaseContents().deserialize(db) for name, db in data["databases"].items()}
        )

    @staticmethod
    def filename_for_token(token: str) -> str:
        """
        Get the filename for the database library for a given token.
        """
        assert token is not None
        assert token != ""
        return f"database_library_{token}.pkl"

    @staticmethod
    def reset(token: str) -> None:
        """
        Reset the database library for a given token.
        This will delete the file associated with the token.
        """
        file_path = DatabaseLibrary.filename_for_token(token)
        if os.path.isfile(file_path):
            os.remove(file_path)

    @staticmethod
    def read_from_file(token: str) -> "DatabaseLibrary":
        """
        Read the database library from a file.
        If the file does not exist, return an empty database library.
        """
        library = DatabaseLibrary()

        file_path = DatabaseLibrary.filename_for_token(token)

        if not os.path.isfile(file_path):
            # File doesn't exist — return empty instance
            return library

        try:
            with open(file_path, "rb") as f:
                # use pickle
                data = pickle.load(f)
                library.deserialize(data)
        except Exception as e:
            raise RuntimeError(f"Failed to read database library from {file_path}: {e}") from e

        return library

    def write_to_file(self, token: str) -> None:
        """
        Write the database library to a temp file, then atomically rename to the destination.
        """
        file_path = DatabaseLibrary.filename_for_token(token)

        data = self.serialize()
        # use pickle
        dir_name = os.path.dirname(file_path) or "."

        with tempfile.NamedTemporaryFile("wb", dir=dir_name, delete=False) as tmp_file:
            pickle.dump(data, tmp_file)
        os.replace(tmp_file.name, file_path)


class DatabaseLibraryContext:
    def __init__(self, token: str, readonly: bool = False) -> None:
        self.token = token
        self.readonly = readonly

    def __enter__(self) -> DatabaseLibrary:
        self.db = DatabaseLibrary.read_from_file(self.token)
        return self.db

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type:
            print(f"An error occurred: {exc_val}")
            # Optionally return True to suppress the exception
            return
        if not self.readonly:
            self.db.write_to_file(self.token)


def add_handler(table: pa.Table) -> pa.Array:
    assert table.num_columns == 2
    return pc.add(table.column(0), table.column(1))


def no_op_handler(table: pa.Table) -> pa.Array:
    return table.column(0)


def time_handler(table: pa.Table) -> pa.Array:
    current_time = datetime.datetime.now(datetime.timezone.utc)
    return pa.array([current_time] * table.num_rows, type=pa.timestamp("us"))


def uppercase_handler(table: pa.Table) -> pa.Array:
    assert table.num_columns == 1
    return pc.utf8_upper(table.column(0))


def any_type_handler(table: pa.Table) -> pa.Array:
    return table.column(0)


def echo_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> Generator[pa.RecordBatch, pa.RecordBatch, None]:
    # Just echo the parameters back as a single row.
    assert parameters.parameters
    yield pa.RecordBatch.from_arrays(
        [parameters.parameters.column(0)],
        schema=pa.schema([pa.field("result", pa.string())]),
    )


def long_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> Generator[pa.RecordBatch, pa.RecordBatch, None]:
    assert parameters.parameters
    for i in range(100):
        yield pa.RecordBatch.from_arrays([[f"{i}"] * 3000] * len(output_schema), schema=output_schema)


def repeat_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> Generator[pa.RecordBatch, pa.RecordBatch, None]:
    # Just echo the parameters back as a single row.
    assert parameters.parameters
    for _i in range(parameters.parameters.column(1).to_pylist()[0]):
        yield pa.RecordBatch.from_arrays(
            [parameters.parameters.column(0)],
            schema=output_schema,
        )


def wide_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> Generator[pa.RecordBatch, pa.RecordBatch, None]:
    # Just echo the parameters back as a single row.
    assert parameters.parameters
    rows = []
    for _i in range(parameters.parameters.column(0).to_pylist()[0]):
        rows.append({f"result_{idx}": idx for idx in range(20)})

    yield pa.RecordBatch.from_pylist(rows, schema=output_schema)


def dynamic_schema_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> Generator[pa.RecordBatch, pa.RecordBatch, None]:
    yield parameters.parameters


def dynamic_schema_handler_output_schema(
    parameters: pa.RecordBatch, input_schema: pa.Schema | None = None
) -> pa.Schema:
    # This is the schema that will be returned to the client.
    # It will be used to create the table function.
    assert isinstance(parameters, pa.RecordBatch)
    return parameters.schema


def in_out_long_schema_handler(parameters: pa.RecordBatch, input_schema: pa.Schema | None = None) -> pa.Schema:
    assert input_schema is not None
    return pa.schema([input_schema.field(0)])


def in_out_schema_handler(parameters: pa.RecordBatch, input_schema: pa.Schema | None = None) -> pa.Schema:
    assert input_schema is not None
    return pa.schema([parameters.schema.field(0), input_schema.field(0)])


def in_out_wide_schema_handler(parameters: pa.RecordBatch, input_schema: pa.Schema | None = None) -> pa.Schema:
    assert input_schema is not None
    return pa.schema([pa.field(f"result_{i}", pa.int32()) for i in range(20)])


def in_out_echo_schema_handler(parameters: pa.RecordBatch, input_schema: pa.Schema | None = None) -> pa.Schema:
    assert input_schema is not None
    return input_schema


def in_out_echo_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> Generator[pa.RecordBatch, pa.RecordBatch, None]:
    result = output_schema.empty_table()

    while True:
        input_chunk = yield result

        if input_chunk is None:
            break

        result = input_chunk

    return


def in_out_wide_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> parameter_types.TableFunctionInOutGenerator:
    result = output_schema.empty_table()

    while True:
        input_chunk = yield (result, True)

        if input_chunk is None:
            break

        if isinstance(input_chunk, bool):
            raise NotImplementedError("Not expecting continuing output for input chunk.")

        chunk_length = len(input_chunk)

        result = pa.RecordBatch.from_arrays(
            [[i] * chunk_length for i in range(20)],
            schema=output_schema,
        )

    return None


def in_out_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> parameter_types.TableFunctionInOutGenerator:
    result = output_schema.empty_table()

    while True:
        input_chunk = yield (result, True)

        if input_chunk is None:
            break

        if isinstance(input_chunk, bool):
            raise NotImplementedError("Not expecting continuing output for input chunk.")

        assert parameters.parameters is not None
        parameter_value = parameters.parameters.column(0).to_pylist()[0]

        # Since input chunks could be different sizes, standardize it.
        result = pa.RecordBatch.from_arrays(
            [
                [parameter_value] * len(input_chunk),
                input_chunk.column(0),
            ],
            schema=output_schema,
        )

    return [pa.RecordBatch.from_arrays([["last"], ["row"]], schema=output_schema)]


def in_out_long_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> parameter_types.TableFunctionInOutGenerator:
    result = output_schema.empty_table()

    while True:
        input_chunk = yield (result, True)

        if input_chunk is None:
            break

        if isinstance(input_chunk, bool):
            raise NotImplementedError("Not expecting continuing output for input chunk.")

        # Return the input chunk ten times.
        multiplier = 10
        copied_results = [
            pa.RecordBatch.from_arrays(
                [
                    input_chunk.column(0),
                ],
                schema=output_schema,
            )
            for index in range(multiplier)
        ]

        for item in copied_results[0:-1]:
            yield (item, False)
        result = copied_results[-1]

    return None


def in_out_huge_chunk_handler(
    parameters: parameter_types.TableFunctionParameters,
    output_schema: pa.Schema,
) -> parameter_types.TableFunctionInOutGenerator:
    result = output_schema.empty_table()
    multiplier = 10
    chunk_length = 5000

    while True:
        input_chunk = yield (result, True)

        if input_chunk is None:
            break

        if isinstance(input_chunk, bool):
            raise NotImplementedError("Not expecting continuing output for input chunk.")

        for index, _i in enumerate(range(multiplier)):
            output = pa.RecordBatch.from_arrays(
                [list(range(chunk_length)), list([index] * chunk_length)],
                schema=output_schema,
            )
            if index < multiplier - 1:
                yield (output, False)
            else:
                result = output

    # test big chunks returned as the last results.
    return [
        pa.RecordBatch.from_arrays([list(range(chunk_length)), list([footer_id] * chunk_length)], schema=output_schema)
        for footer_id in (-1, -2, -3)
    ]


def yellow_taxi_endpoint_generator(ticket_data: Any) -> list[flight.FlightEndpoint]:
    """
    Generate a list of FlightEndpoint objects for the NYC Yellow Taxi dataset.
    """
    files = [
        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-01.parquet",
        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-02.parquet",
        #        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-03.parquet",
        #        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-04.parquet",
        #        "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2025-05.parquet",
    ]
    return [
        flight_handling.endpoint(
            ticket_data=ticket_data,
            locations=[
                flight_handling.dict_to_msgpack_duckdb_call_data_uri(
                    {
                        "function_name": "read_parquet",
                        # So arguments could be a record batch.
                        "data": flight_handling.serialize_arrow_ipc_table(
                            pa.Table.from_pylist(
                                [
                                    {
                                        "arg_0": files,
                                        "hive_partitioning": False,
                                        "union_by_name": True,
                                    }
                                ],
                            )
                        ),
                    }
                )
            ],
        )
    ]


remote_data_schema = SchemaCollection(
    scalar_functions_by_name=CaseInsensitiveDict(),
    table_functions_by_name=CaseInsensitiveDict(),
    tables_by_name=CaseInsensitiveDict(
        {
            "nyc_yellow_taxi": TableInfo(
                table_versions=[
                    pa.schema(
                        [
                            pa.field("VendorID", pa.int32()),
                            pa.field("tpep_pickup_datetime", pa.timestamp("us")),
                            pa.field("tpep_dropoff_datetime", pa.timestamp("us")),
                            pa.field("passenger_count", pa.int64()),
                            pa.field("trip_distance", pa.float64()),
                            pa.field("RatecodeID", pa.int64()),
                            pa.field("store_and_fwd_flag", pa.string()),
                            pa.field("PULocationID", pa.int32()),
                            pa.field("DOLocationID", pa.int32()),
                            pa.field("payment_type", pa.int64()),
                            pa.field("fare_amount", pa.float64()),
                            pa.field("extra", pa.float64()),
                            pa.field("mta_tax", pa.float64()),
                            pa.field("tip_amount", pa.float64()),
                            pa.field("tolls_amount", pa.float64()),
                            pa.field("improvement_surcharge", pa.float64()),
                            pa.field("total_amount", pa.float64()),
                            pa.field("congestion_surcharge", pa.float64()),
                            pa.field("Airport_fee", pa.float64()),
                            pa.field("cbd_congestion_fee", pa.float64()),
                        ]
                    ).empty_table()
                ],
                row_id_counter=0,
                endpoint_generator=yellow_taxi_endpoint_generator,
            ),
        }
    ),
)


static_data_schema = SchemaCollection(
    scalar_functions_by_name=CaseInsensitiveDict(),
    table_functions_by_name=CaseInsensitiveDict(),
    tables_by_name=CaseInsensitiveDict(
        {
            "big_chunk": TableInfo(
                table_versions=[
                    pa.Table.from_arrays(
                        [
                            list(range(100000)),
                        ],
                        schema=pa.schema([pa.field("id", pa.int64())]),
                    )
                ],
                row_id_counter=0,
            ),
            "employees": TableInfo(
                table_versions=[
                    pa.Table.from_arrays(
                        [
                            ["Emily", "Amy"],
                            [30, 32],
                            [datetime.datetime(2023, 10, 1), datetime.datetime(2024, 10, 2)],
                            ["{}", "[1,2,3]"],
                            [
                                bytes.fromhex("b975e4187a6d4afdb1a41f7174ce1805"),
                                bytes.fromhex("7ef19ab7c7af4f0188c386fae862fd60"),
                            ],
                            [datetime.date(2023, 10, 1), datetime.date(2024, 10, 2)],
                            [True, False],
                            ["Ann", None],
                            [1234.123, 5678.123],
                            [Decimal("12345.678790"), Decimal("67890.123456")],
                        ],
                        schema=pa.schema(
                            [
                                pa.field("name", pa.string()),
                                pa.field("age", pa.int32()),
                                pa.field("start_date", pa.timestamp("ms")),
                                pa.field("json_data", pa.json_(pa.string())),
                                pa.field("id", pa.uuid()),
                                pa.field("birthdate", pa.date32()),
                                pa.field("is_active", pa.bool_()),
                                pa.field("nickname", pa.string()),
                                pa.field("salary", pa.float64()),
                                pa.field("balance", pa.decimal128(12, 6)),
                            ],
                            metadata={"can_produce_statistics": "1"},
                        ),
                    )
                ],
                row_id_counter=2,
            ),
        }
    ),
)


def collatz_step_count(n: int) -> int:
    steps = 0
    while n != 1:
        if n % 2 == 0:
            n //= 2
        else:
            n = 3 * n + 1
        steps += 1
    return steps


def collatz(inputs: pa.Array) -> pa.Array:
    results = [collatz_step_count(n) for n in inputs.to_pylist()]
    return pa.array(results, type=pa.int64())


def collatz_steps(n: int) -> list[int]:
    steps = 0
    results = []
    while n != 1:
        if n % 2 == 0:
            n //= 2
        else:
            n = 3 * n + 1
        results.append(n)
        steps += 1
    return results


util_schema = SchemaCollection(
    scalar_functions_by_name=CaseInsensitiveDict(
        {
            "test_uppercase": ScalarFunction(
                input_schema=pa.schema([pa.field("a", pa.string())]),
                output_schema=pa.schema([pa.field("result", pa.string())]),
                handler=uppercase_handler,
                stability="consistent",
            ),
            "test_any_type": ScalarFunction(
                input_schema=pa.schema([pa.field("a", pa.string(), metadata={"is_any_type": "1"})]),
                output_schema=pa.schema([pa.field("result", pa.string())]),
                handler=any_type_handler,
                stability="consistent",
            ),
            "test_time": ScalarFunction(
                input_schema=pa.schema([]),
                output_schema=pa.schema([pa.field("result", pa.timestamp("us"))]),
                handler=time_handler,
                stability="consistent_within_query",
            ),
            "test_no_op": ScalarFunction(
                input_schema=pa.schema([pa.field("a", pa.int64(), metadata={"is_any_type": "1"})]),
                output_schema=pa.schema([pa.field("result", pa.int64())]),
                handler=no_op_handler,
                stability="consistent",
            ),
            "test_no_op_string": ScalarFunction(
                input_schema=pa.schema([pa.field("a", pa.string())]),
                output_schema=pa.schema([pa.field("result", pa.string())]),
                handler=no_op_handler,
                stability="consistent",
            ),
            "test_add": ScalarFunction(
                input_schema=pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.int64())]),
                output_schema=pa.schema([pa.field("result", pa.int64())]),
                handler=add_handler,
                stability="consistent",
            ),
            "collatz": ScalarFunction(
                input_schema=pa.schema([pa.field("n", pa.int64())]),
                output_schema=pa.schema([pa.field("result", pa.int64())]),
                handler=lambda table: collatz(table.column(0)),
                stability="consistent",
            ),
            "collatz_sequence": ScalarFunction(
                input_schema=pa.schema([pa.field("n", pa.int64())]),
                output_schema=pa.schema([pa.field("result", pa.list_(pa.int64()))]),
                handler=lambda table: pa.array(
                    [collatz_steps(n) for n in table.column(0).to_pylist()], type=pa.list_(pa.int64())
                ),
                stability="consistent",
            ),
        }
    ),
    table_functions_by_name=CaseInsensitiveDict(
        {
            "test_echo": TableFunction(
                input_schema=pa.schema([pa.field("input", pa.string())]),
                output_schema_source=pa.schema([pa.field("result", pa.string())]),
                handler=echo_handler,
            ),
            "test_wide": TableFunction(
                input_schema=pa.schema([pa.field("count", pa.int32())]),
                output_schema_source=pa.schema([pa.field(f"result_{i}", pa.int32()) for i in range(20)]),
                handler=wide_handler,
            ),
            "test_long": TableFunction(
                input_schema=pa.schema([pa.field("input", pa.string())]),
                output_schema_source=pa.schema(
                    [
                        pa.field("result", pa.string()),
                        pa.field("result2", pa.string()),
                    ]
                ),
                handler=long_handler,
            ),
            "test_repeat": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field("input", pa.string()),
                        pa.field("count", pa.int32()),
                    ]
                ),
                output_schema_source=pa.schema([pa.field("result", pa.string())]),
                handler=repeat_handler,
            ),
            "test_dynamic_schema": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field(
                            "input",
                            pa.string(),
                            metadata={"is_any_type": "1"},
                        )
                    ]
                ),
                output_schema_source=TableFunctionDynamicOutput(
                    schema_creator=dynamic_schema_handler_output_schema,
                    default_values=(
                        pa.RecordBatch.from_arrays(
                            [pa.array([1], type=pa.int32())],
                            schema=pa.schema([pa.field("input", pa.int32())]),
                        ),
                        None,
                    ),
                ),
                handler=dynamic_schema_handler,
            ),
            "test_dynamic_schema_named_parameters": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field("name", pa.string()),
                        pa.field(
                            "location",
                            pa.string(),
                            metadata={"is_named_parameter": "1"},
                        ),
                        pa.field(
                            "input",
                            pa.string(),
                            metadata={"is_any_type": "1"},
                        ),
                        pa.field("city", pa.string()),
                    ]
                ),
                output_schema_source=TableFunctionDynamicOutput(
                    schema_creator=dynamic_schema_handler_output_schema,
                    default_values=(
                        pa.RecordBatch.from_arrays(
                            [pa.array([1], type=pa.int32())],
                            schema=pa.schema([pa.field("input", pa.int32())]),
                        ),
                        None,
                    ),
                ),
                handler=dynamic_schema_handler,
            ),
            "test_table_in_out": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field("input", pa.string()),
                        pa.field(
                            "table_input",
                            pa.string(),
                            metadata={"is_table_type": "1"},
                        ),
                    ]
                ),
                output_schema_source=TableFunctionDynamicOutput(
                    schema_creator=in_out_schema_handler,
                    default_values=(
                        pa.RecordBatch.from_arrays(
                            [pa.array([1], type=pa.int32())],
                            schema=pa.schema([pa.field("input", pa.int32())]),
                        ),
                        pa.schema([pa.field("input", pa.int32())]),
                    ),
                ),
                handler=in_out_handler,
            ),
            "test_table_in_out_long": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field(
                            "table_input",
                            pa.string(),
                            metadata={"is_table_type": "1"},
                        ),
                    ]
                ),
                output_schema_source=TableFunctionDynamicOutput(
                    schema_creator=in_out_long_schema_handler,
                    default_values=(
                        pa.RecordBatch.from_arrays(
                            [pa.array([1], type=pa.int32())],
                            schema=pa.schema([pa.field("input", pa.int32())]),
                        ),
                        pa.schema([pa.field("input", pa.int32())]),
                    ),
                ),
                handler=in_out_long_handler,
            ),
            "test_table_in_out_huge": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field(
                            "table_input",
                            pa.string(),
                            metadata={"is_table_type": "1"},
                        ),
                    ]
                ),
                output_schema_source=pa.schema([("multiplier", pa.int64()), ("value", pa.int64())]),
                handler=in_out_huge_chunk_handler,
            ),
            "test_table_in_out_wide": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field("input", pa.string()),
                        pa.field(
                            "table_input",
                            pa.string(),
                            metadata={"is_table_type": "1"},
                        ),
                    ]
                ),
                output_schema_source=TableFunctionDynamicOutput(
                    schema_creator=in_out_wide_schema_handler,
                    default_values=(
                        pa.RecordBatch.from_arrays(
                            [pa.array([1], type=pa.int32())],
                            schema=pa.schema([pa.field("input", pa.int32())]),
                        ),
                        pa.schema([pa.field("input", pa.int32())]),
                    ),
                ),
                handler=in_out_wide_handler,
            ),
            "test_table_in_out_echo": TableFunction(
                input_schema=pa.schema(
                    [
                        pa.field(
                            "table_input",
                            pa.string(),
                            metadata={"is_table_type": "1"},
                        ),
                    ]
                ),
                output_schema_source=TableFunctionDynamicOutput(
                    schema_creator=in_out_echo_schema_handler,
                    default_values=(
                        pa.RecordBatch.from_arrays(
                            [pa.array([1], type=pa.int32())],
                            schema=pa.schema([pa.field("input", pa.int32())]),
                        ),
                        pa.schema([pa.field("input", pa.int32())]),
                    ),
                ),
                handler=in_out_echo_handler,
            ),
        }
    ),
)
