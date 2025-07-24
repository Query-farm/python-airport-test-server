import hashlib
import json
import re
from collections.abc import Generator, Iterator
from typing import Any, TypeVar

import click
import duckdb
import msgpack
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.flight as flight
import query_farm_duckdb_json_serialization.expression
import query_farm_flight_server.auth as auth
import query_farm_flight_server.auth_manager as auth_manager
import query_farm_flight_server.auth_manager_naive as auth_manager_naive
import query_farm_flight_server.flight_handling as flight_handling
import query_farm_flight_server.flight_inventory as flight_inventory
import query_farm_flight_server.middleware as base_middleware
import query_farm_flight_server.parameter_types as parameter_types
import query_farm_flight_server.schema_uploader as schema_uploader
import query_farm_flight_server.server as base_server
import structlog
from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from .database_impl import (
    DatabaseContents,
    DatabaseLibrary,
    DatabaseLibraryContext,
    SchemaCollection,
    TableInfo,
    descriptor_unpack_,
)

log = structlog.get_logger()


def read_recordbatch(source: bytes) -> pa.RecordBatch:
    """
    Read a record batch from a byte string.
    """
    buffer = pa.BufferReader(source)
    ipc_stream = pa.ipc.open_stream(buffer)
    return next(ipc_stream)


def conform_nullable(schema: pa.Schema, table: pa.Table) -> pa.Table:
    """
    Conform the table to the nullable flags as defined in the schema.

    There shouldn't be null values in the columns.

    This is needed because DuckDB doesn't send the nullable flag in the schema
    it sends via the DoExchange call.
    """
    for idx, table_field in enumerate(schema):
        if not table_field.nullable:
            # Only update the column if the new schema allows nulls where the original did not
            new_field = table_field.with_nullable(False)

            # Check for null values.
            if table.column(idx).null_count > 0:
                raise flight.FlightServerError(
                    f"Column {table_field.name} has null values, but the schema does not allow nulls."
                )

            table = table.set_column(idx, new_field, table.column(idx))
    return table


def check_schema_is_subset_of_schema(existing_schema: pa.Schema, new_schema: pa.Schema) -> None:
    """
    Check that the new schema is a subset of the existing schema.
    """
    existing_contents = set([(field.name, field.type) for field in existing_schema])
    new_contents = set([(field.name, field.type) for field in new_schema])

    unknown_fields = new_contents - existing_contents
    if unknown_fields:
        raise flight.FlightServerError(f"Unknown fields in insert: {unknown_fields}")
    return


# class FlightTicketDataTableFunction(flight_handling.FlightTicketData):
#     action_name: str
#     schema_name: str
#     parameters: bytes


# def model_selector(flight_name: str, src: bytes) -> flight_handling.FlightTicketData | FlightTicketDataTableFunction:
#     return flight_handling.FlightTicketData.unpack(src)


class FlightTicketData(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)  # for Pydantic v2
    descriptor: flight.FlightDescriptor

    where_clause: str | None = None

    # These are the parameters for the table returning function.
    table_function_parameters: pa.RecordBatch | None = None
    table_function_input_schema: pa.Schema | None = None

    at_unit: str | None = None
    at_value: str | None = None

    _validate_table_function_parameters = field_validator("table_function_parameters", mode="before")(
        parameter_types.deserialize_record_batch_or_none
    )

    _validate_table_function_input_schema = field_validator("table_function_input_schema", mode="before")(
        parameter_types.deserialize_schema_or_none
    )

    @field_serializer("table_function_parameters")
    def serialize_table_function_parameters(self, value: pa.RecordBatch, info: Any) -> bytes | None:
        return parameter_types.serialize_record_batch(value, info)

    @field_serializer("table_function_input_schema")
    def serialize_table_function_input_Schema(self, value: pa.RecordBatch, info: Any) -> bytes | None:
        return parameter_types.serialize_schema(value, info)

    _validate_flight_descriptor = field_validator("descriptor", mode="before")(
        parameter_types.deserialize_flight_descriptor
    )

    @field_serializer("descriptor")
    def serialize_flight_descriptor(self, value: flight.FlightDescriptor, info: Any) -> bytes:
        return parameter_types.serialize_flight_descriptor(value, info)


T = TypeVar("T", bound=BaseModel)


class InMemoryArrowFlightServer(base_server.BasicFlightServer[auth.Account, auth.AccountToken]):
    def __init__(
        self,
        *,
        location: str | None,
        auth_manager: auth_manager.AuthManager[auth.Account, auth.AccountToken],
        **kwargs: dict[str, Any],
    ) -> None:
        self.service_name = "test_server"
        self._auth_manager = auth_manager
        super().__init__(location=location, **kwargs)

        self.ROWID_FIELD_NAME = "rowid"
        self.rowid_field = pa.field(self.ROWID_FIELD_NAME, pa.int64(), metadata={"is_rowid": "1"})

    def action_endpoints(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.Endpoints,
    ) -> list[flight.FlightEndpoint]:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(parameters.descriptor)
        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)

            filter_sql_where_clause: str | None = None
            if parameters.parameters.json_filters is not None:
                context.logger.debug("duckdb_input", input=json.dumps(parameters.parameters.json_filters.filters))
                filter_sql_where_clause, filter_sql_field_type_info = (
                    query_farm_duckdb_json_serialization.expression.convert_to_sql(
                        source=parameters.parameters.json_filters.filters,
                        bound_column_names=parameters.parameters.json_filters.column_binding_names_by_index,
                    )
                )
                if filter_sql_where_clause == "":
                    filter_sql_where_clause = None

            if descriptor_parts.type == "table":
                table_info = schema.by_name("table", descriptor_parts.name)

                ticket_data = FlightTicketData(
                    descriptor=parameters.descriptor,
                    where_clause=filter_sql_where_clause,
                    at_unit=parameters.parameters.at_unit,
                    at_value=parameters.parameters.at_value,
                )

                if table_info.endpoint_generator is not None:
                    return table_info.endpoint_generator(ticket_data)
                return [flight_handling.endpoint(ticket_data=ticket_data, locations=None)]
            elif descriptor_parts.type == "table_function":
                # So the table function may not exist, because its a dynamic descriptor.

                schema.by_name("table_function", descriptor_parts.name)

                ticket_data = FlightTicketData(
                    descriptor=parameters.descriptor,
                    where_clause=filter_sql_where_clause,
                    table_function_parameters=parameters.parameters.table_function_parameters,
                    table_function_input_schema=parameters.parameters.table_function_input_schema,
                    at_unit=parameters.parameters.at_unit,
                    at_value=parameters.parameters.at_value,
                )
                return [flight_handling.endpoint(ticket_data=ticket_data, locations=None)]
            else:
                raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")

    def action_list_schemas(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.ListSchemas,
    ) -> base_server.AirportSerializedCatalogRoot:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:
            database = library.by_name(parameters.catalog_name)

            dynamic_inventory: dict[str, dict[str, list[flight_inventory.FlightInventoryWithMetadata]]] = {}

            catalog_contents = dynamic_inventory.setdefault(parameters.catalog_name, {})

            for schema_name, schema in database.schemas_by_name.items():
                schema_contents = catalog_contents.setdefault(schema_name, [])
                for coll in schema.containers():
                    for name, obj in coll.items():
                        schema_contents.append(
                            obj.flight_info(
                                name=name,
                                catalog_name=parameters.catalog_name,
                                schema_name=schema_name,
                            )
                        )

            return flight_inventory.upload_and_generate_schema_list(
                upload_parameters=flight_inventory.UploadParameters(
                    s3_client=None,
                    base_url="http://localhost",
                    bucket_name="test_bucket",
                    bucket_prefix="test_prefix",
                ),
                flight_service_name=self.service_name,
                flight_inventory=dynamic_inventory,
                schema_details={},
                skip_upload=True,
                serialize_inline=True,
                catalog_version=1,
                catalog_version_fixed=False,
            )

    def impl_list_flights(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        criteria: bytes,
    ) -> Iterator[flight.FlightInfo]:
        assert context.caller is not None
        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:

            def yield_flight_infos() -> Generator[flight.FlightInfo, None, None]:
                for db_name, db in library.databases_by_name.items():
                    for schema_name, schema in db.schemas_by_name.items():
                        for coll in schema.containers():
                            for name, obj in coll.items():
                                yield obj.flight_info(name=name, catalog_name=db_name, schema_name=schema_name)[0]

            return yield_flight_infos()

    def impl_get_flight_info(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        descriptor: flight.FlightDescriptor,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(descriptor)
        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)

            obj = schema.by_name(descriptor_parts.type, descriptor_parts.name)
            return obj.flight_info(
                name=descriptor_parts.name,
                catalog_name=descriptor_parts.catalog_name,
                schema_name=descriptor_parts.schema_name,
            )[0]

    def action_catalog_version(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.CatalogVersion,
    ) -> base_server.GetCatalogVersionResult:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:
            database = library.by_name(parameters.catalog_name)

            context.logger.debug(
                "catalog_version_result",
                catalog_name=parameters.catalog_name,
                version=database.version,
            )
            return base_server.GetCatalogVersionResult(catalog_version=database.version, is_fixed=False)

    def action_create_transaction(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.CreateTransaction,
    ) -> base_server.CreateTransactionResult:
        return base_server.CreateTransactionResult(identifier=None)

    def action_create_schema(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.CreateSchema,
    ) -> base_server.AirportSerializedContentsWithSHA256Hash:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog_name)

            if database.schemas_by_name.get(parameters.schema_name) is not None:
                raise flight.FlightServerError(f"Schema {parameters.schema_name} already exists")

            database.schemas_by_name[parameters.schema_name] = SchemaCollection()
            database.version += 1

            # FIXME: this needs to be handled better on the server side...
            # rather than calling into internal methods.
            packed_data = msgpack.packb([])
            assert packed_data
            compressed_data = schema_uploader._compress_and_prefix_with_length(packed_data, compression_level=3)

            empty_hash = hashlib.sha256(compressed_data).hexdigest()
            return base_server.AirportSerializedContentsWithSHA256Hash(
                url=None, sha256=empty_hash, serialized=compressed_data
            )

    def action_drop_table(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.DropObject,
    ) -> None:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog_name)
            schema = database.by_name(parameters.schema_name)

            schema.by_name("table", parameters.name)

            del schema.tables_by_name[parameters.name]
            database.version += 1

    def action_drop_schema(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.DropObject,
    ) -> None:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog_name)

            if database.schemas_by_name.get(parameters.name) is None:
                raise flight.FlightServerError(f"Schema '{parameters.name}' does not exist")

            del database.schemas_by_name[parameters.name]
            database.version += 1

    def action_create_table(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.CreateTable,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog_name)
            schema = database.by_name(parameters.schema_name)

            if parameters.table_name in schema.tables_by_name:
                raise flight.FlightServerError(
                    f"Table {parameters.table_name} already exists for token {context.caller.token}"
                )

            # FIXME: may want to add a row_id column that is not visable to the user, so that inserts and
            # deletes can be tested.

            assert "_rowid" not in parameters.arrow_schema.names

            schema_with_row_id = parameters.arrow_schema.append(self.rowid_field)

            table_info = TableInfo(
                table_versions=[schema_with_row_id.empty_table()],
                row_id_counter=0,
                primary_key_columns=parameters.primary_key_columns,
                unique_columns=parameters.unique_columns,
                multi_key_primary_keys=parameters.multi_key_primary_keys,
                extra_constraints=parameters.extra_constraints,
            )

            schema.tables_by_name[parameters.table_name] = table_info

            database.version += 1

        return table_info.flight_info(
            name=parameters.table_name,
            catalog_name=parameters.catalog_name,
            schema_name=parameters.schema_name,
        )[0]

    def impl_do_action(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        action: flight.Action,
    ) -> Iterator[bytes]:
        assert context.caller is not None

        if action.type == "reset":
            context.logger.debug("Resetting server state")
            DatabaseLibrary.reset(context.caller.token.token)
            return iter([])
        elif action.type == "generate_error":
            error_name = action.body.to_pybytes().decode("utf-8")
            known_errors = {
                "flight_unavailable": flight.FlightUnavailableError,
                "flight_server_error": flight.FlightServerError,
                "flight_unauthenticated": flight.FlightUnauthenticatedError,
            }
            if error_name in known_errors:
                raise known_errors[error_name](f"Testing error: {error_name}")
            else:
                context.logger.error("Unknown error type", error_name=error_name)
                raise flight.FlightServerError(f"Unknown error type: {error_name}")
        elif action.type == "create_database":
            database_name = action.body.to_pybytes().decode("utf-8")
            context.logger.debug("Creating database", database_name=database_name)
            with DatabaseLibraryContext(context.caller.token.token) as library:
                if database_name in library.databases_by_name:
                    raise flight.FlightServerError(f"Database {database_name} already exists")
                library.databases_by_name[database_name] = DatabaseContents()
            return iter([])
        elif action.type == "drop_database":
            database_name = action.body.to_pybytes().decode("utf-8")
            context.logger.debug("Dropping database", database_name=database_name)

            with DatabaseLibraryContext(context.caller.token.token) as library:
                if action.body.decode("utf-8") not in library.databases_by_name:
                    raise flight.FlightServerError(f"Database {action.body.decode('utf-8')} does not exist")
                del library.databases_by_name[action.body.decode("utf-8")]
            return iter([])

        raise flight.FlightServerError(f"Unsupported action type: {action.type}")

    def exchange_update(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        descriptor: flight.FlightDescriptor,
        reader: flight.MetadataRecordBatchReader,
        writer: flight.MetadataRecordBatchWriter,
        return_chunks: bool,
    ) -> int:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(descriptor)

        if descriptor_parts.type != "table":
            raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)
            table_info = schema.by_name("table", descriptor_parts.name)

            existing_table = table_info.version()

            writer.begin(existing_table.schema)

            rowid_index = existing_table.schema.get_field_index(self.ROWID_FIELD_NAME)
            assert rowid_index != -1

            change_count = 0

            for chunk in reader:
                if chunk.data is not None:
                    chunk_table = pa.Table.from_batches([chunk.data])
                    assert chunk_table.num_rows > 0

                    # So this chunk will contain any updated columns and the row id.

                    input_rowid_index = chunk_table.schema.get_field_index(self.ROWID_FIELD_NAME)

                    # To perform an update, first remove the rows from the table,
                    # then assign new row ids to the incoming rows, and append them to the table.
                    table_mask = pc.is_in(
                        existing_table.column(rowid_index),
                        value_set=chunk_table.column(input_rowid_index),
                    )

                    # This is the table with updated rows removed, we'll be adding rows to it later on.
                    table_without_updated_rows = pc.filter(existing_table, pc.invert(table_mask))

                    # These are the rows that are being updated, since the update may not send all
                    # the columns, we need to filter the table to get the updated rows to persist the
                    # values that aren't being updated.
                    changed_rows = pc.filter(existing_table, table_mask)

                    # Get a list of all of the columns that are not being updated, so that a join
                    # can be performed.
                    unchanged_column_names = set(existing_table.schema.names) - set(chunk_table.schema.names)

                    joined_table = pa.Table.join(
                        changed_rows.select(list(unchanged_column_names) + [self.ROWID_FIELD_NAME]),
                        chunk_table,
                        keys=[self.ROWID_FIELD_NAME],
                        join_type="inner",
                    )

                    # Add the new row id column.
                    chunk_length = len(joined_table)
                    rowid_values = [
                        x
                        for x in range(
                            table_info.row_id_counter,
                            table_info.row_id_counter + chunk_length,
                        )
                    ]
                    updated_rows = joined_table.set_column(
                        joined_table.schema.get_field_index(self.ROWID_FIELD_NAME),
                        self.rowid_field,
                        [rowid_values],
                    )
                    table_info.row_id_counter += chunk_length

                    # Now the columns may be in a different order, so we need to realign them.
                    updated_rows = updated_rows.select(existing_table.schema.names)

                    check_schema_is_subset_of_schema(existing_table.schema, updated_rows.schema)

                    updated_rows = conform_nullable(existing_table.schema, updated_rows)

                    updated_table = pa.concat_tables(
                        [
                            table_without_updated_rows,
                            updated_rows.select(table_without_updated_rows.schema.names),
                        ]
                    )

                    if return_chunks:
                        writer.write_table(updated_rows)

                    existing_table = updated_table

            table_info.update_table(existing_table)

        return change_count

    def exchange_delete(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        descriptor: flight.FlightDescriptor,
        reader: flight.MetadataRecordBatchReader,
        writer: flight.MetadataRecordBatchWriter,
        return_chunks: bool,
    ) -> int:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(descriptor)

        if descriptor_parts.type != "table":
            raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")
        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)
            table_info = schema.by_name("table", descriptor_parts.name)

            existing_table = table_info.version()
            writer.begin(existing_table.schema)

            rowid_index = existing_table.schema.get_field_index(self.ROWID_FIELD_NAME)
            assert rowid_index != -1

            change_count = 0

            for chunk in reader:
                if chunk.data is not None:
                    chunk_table = pa.Table.from_batches([chunk.data])
                    assert chunk_table.num_rows > 0

                    # Should only be getting the row id.
                    assert chunk_table.num_columns == 1
                    # the rowid field doesn't come back the same since it missing the
                    # not null flag and the metadata, so can't compare the schemas
                    input_rowid_index = chunk_table.schema.get_field_index(self.ROWID_FIELD_NAME)

                    # Now do an antijoin to get the rows that are not in the delete_rows.
                    target_rowids = chunk_table.column(input_rowid_index)
                    existing_row_ids = existing_table.column(rowid_index)

                    mask = pc.is_in(existing_row_ids, value_set=target_rowids)

                    target_rows = pc.filter(existing_table, mask)
                    changed_table = pc.filter(existing_table, pc.invert(mask))

                    change_count += target_rows.num_rows

                    if return_chunks:
                        writer.write_table(target_rows)

                    existing_table = changed_table

            table_info.update_table(existing_table)

        return change_count

    def exchange_insert(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        descriptor: flight.FlightDescriptor,
        reader: flight.MetadataRecordBatchReader,
        writer: flight.MetadataRecordBatchWriter,
        return_chunks: bool,
    ) -> int:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(descriptor)

        if descriptor_parts.type != "table":
            raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)
            table_info = schema.by_name("table", descriptor_parts.name)

            existing_table = table_info.version()
            writer.begin(existing_table.schema)
            change_count = 0

            rowid_index = existing_table.schema.get_field_index(self.ROWID_FIELD_NAME)
            assert rowid_index != -1

            # Check that the data being read matches the table without the rowid column.

            # DuckDB won't send field metadata when it sends us the schema that it uses
            # to perform an insert, so we need some way to adapt the schema we
            check_schema_is_subset_of_schema(existing_table.schema, reader.schema)

            # FIXME: need to handle the case of rowids.

            for chunk in reader:
                if chunk.data is not None:
                    new_rows = pa.Table.from_batches([chunk.data])

                    for column_name in table_info.primary_key_columns:
                        insert_values = new_rows.column(column_name)

                        # Make sure that all elements are unique
                        value_counts = pc.value_counts(insert_values)
                        duplicates_struct = pc.filter(value_counts, pc.greater(value_counts.field("counts"), 1))
                        duplicates = duplicates_struct.field("values")
                        if len(duplicates) > 0:
                            raise flight.FlightServerError(
                                f"Cannot insert rows with duplicate values in primary key column {column_name}: {duplicates}",
                                extra_info=msgpack.packb(
                                    {
                                        "exception_type": "ConstraintException",
                                        "message": f"""Duplicate key "{column_name}: {duplicates[0]}" violates primary key constraint.""",
                                    }
                                ),
                            )

                        already_exists = pc.is_in(insert_values, value_set=existing_table.column(column_name))
                        existing_rows = pc.filter(insert_values, already_exists)
                        if len(existing_rows) > 0:
                            raise flight.FlightServerError(
                                f"Cannot insert rows with duplicate values in primary key column {column_name}: {duplicates}",
                                extra_info=msgpack.packb(
                                    {
                                        "exception_type": "ConstraintException",
                                        "message": f"""Duplicate key "{column_name}: {existing_rows[0]}" violates primary key constraint.""",
                                    }
                                ),
                            )

                    assert new_rows.num_rows > 0

                    # append the row id column to the new rows.
                    chunk_length = new_rows.num_rows
                    rowid_values = [
                        x
                        for x in range(
                            table_info.row_id_counter,
                            table_info.row_id_counter + chunk_length,
                        )
                    ]
                    new_rows = new_rows.append_column(self.rowid_field, [rowid_values])
                    table_info.row_id_counter += chunk_length
                    change_count += chunk_length

                    if return_chunks:
                        writer.write_table(new_rows)

                    # Since the table could have columns removed and deleted, use .select
                    # the ensure that the columns are aligned in the same order as the original table.

                    # So it turns out that DuckDB doesn't send the "not null" flag in the arrow schema.
                    #
                    # This means we can't concat the tables, without those flags matching.
                    # for field_name in existing_table.schema.names:
                    #     field = existing_table.schema.field(field_name)
                    #     if not field.nullable:
                    #         field_index = new_rows.schema.get_field_index(field_name)
                    #         new_rows = new_rows.set_column(
                    #             field_index, field.with_nullable(False), new_rows.column(field_index)
                    #         )
                    new_rows = conform_nullable(existing_table.schema, new_rows)

                    existing_table = pa.concat_tables([existing_table, new_rows.select(existing_table.schema.names)])

            table_info.update_table(existing_table)

        return change_count

    def exchange_table_function_in_out(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        descriptor: flight.FlightDescriptor,
        parameters: parameter_types.TableFunctionParameters,
        input_schema: pa.Schema,
    ) -> tuple[pa.Schema, Generator[pa.RecordBatch, pa.RecordBatch, pa.RecordBatch]]:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(descriptor)

        if descriptor_parts.type != "table_function":
            raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")
        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)
            table_info = schema.by_name("table_function", descriptor_parts.name)

            output_schema = table_info.output_schema(parameters=parameters.parameters, input_schema=input_schema)
            gen = table_info.handler(parameters, output_schema)
        return (output_schema, gen)

    def action_add_column(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.AddColumn,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            assert len(parameters.column_schema.names) == 1

            existing_table = table_info.version()
            # Don't allow duplicate colum names.
            assert parameters.column_schema.field(0).name not in existing_table.schema.names

            table_info.update_table(
                existing_table.append_column(
                    parameters.column_schema.field(0).name,
                    [
                        pa.nulls(
                            existing_table.num_rows,
                            type=parameters.column_schema.field(0).type,
                        )
                    ],
                )
            )
            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_remove_column(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.RemoveColumn,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            table_info.update_table(table_info.version().drop(parameters.removed_column))
            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_rename_column(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.RenameColumn,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            table_info.update_table(table_info.version().rename_columns({parameters.old_name: parameters.new_name}))
            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_rename_table(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.RenameTable,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            schema.tables_by_name[parameters.new_table_name] = schema.tables_by_name.pop(parameters.name)

            database.version += 1

        return table_info.flight_info(
            name=parameters.new_table_name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_set_default(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.SetDefault,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)
            table_info = schema.by_name("table", parameters.name)

            # Defaults are set as metadata on a field.

            t = table_info.version()
            field_index = t.schema.get_field_index(parameters.column_name)
            field = t.schema.field(parameters.column_name)
            new_metadata: dict[str, Any] = {}
            if field.metadata:
                new_metadata = {**field.metadata}

            new_metadata["default"] = parameters.expression

            table_info.update_table(
                t.set_column(
                    field_index,
                    field.with_metadata(new_metadata),
                    t.column(field_index),
                )
            )

            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_set_not_null(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.SetNotNull,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            t = table_info.version()
            field_index = t.schema.get_field_index(parameters.column_name)
            field = t.schema.field(parameters.column_name)

            if t.column(field_index).null_count > 0:
                raise flight.FlightServerError(f"Cannot set column {parameters.column_name} contains null values")

            table_info.update_table(
                t.set_column(
                    field_index,
                    field.with_nullable(False),
                    t.column(field_index),
                )
            )

            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_drop_not_null(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.DropNotNull,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            t = table_info.version()
            field_index = t.schema.get_field_index(parameters.column_name)
            field = t.schema.field(parameters.column_name)

            table_info.update_table(
                t.set_column(
                    field_index,
                    field.with_nullable(True),
                    t.column(field_index),
                )
            )

            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_change_column_type(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.ChangeColumnType,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(parameters.catalog)
            schema = database.by_name(parameters.schema_name)

            table_info = schema.by_name("table", parameters.name)

            # Defaults are set as metadata on a field.

            t = table_info.version()
            column_name = parameters.column_schema.field(0).name
            field_index = t.schema.get_field_index(column_name)
            field = t.schema.field(column_name)

            new_type = parameters.column_schema.field(0).type
            new_field = pa.field(field.name, new_type, metadata=field.metadata)
            new_data = pc.cast(t.column(field_index), new_type)

            table_info.update_table(
                t.set_column(
                    field_index,
                    new_field,
                    new_data,
                )
            )

            database.version += 1

        return table_info.flight_info(
            name=parameters.name,
            catalog_name=parameters.catalog,
            schema_name=parameters.schema_name,
        )[0]

    def action_column_statistics(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.ColumnStatistics,
    ) -> pa.Table:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(parameters.flight_descriptor)
        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)

            assert descriptor_parts.type == "table"
            table = schema.by_name("table", descriptor_parts.name)

            contents = table.version().column(parameters.column_name)
            # Since the table is a Pyarrow table we need to produce some values.
            not_null_count = pc.count(contents, "only_valid").as_py()
            null_count = pc.count(contents, "only_null").as_py()
            distinct_count = len(set(contents.to_pylist()))
            sorted_contents = sorted(filter(lambda x: x is not None, contents.to_pylist()))
            min_value = sorted_contents[0]
            max_value = sorted_contents[-1]

            additional_values = {}
            additional_schema_fields = []
            if contents.type in (pa.string(), pa.utf8(), pa.binary()):
                max_length = pc.max(pc.binary_length(contents)).as_py()

                additional_values = {"max_string_length": max_length, "contains_unicode": contents.type == pa.utf8()}
                additional_schema_fields = [
                    pa.field("max_string_length", pa.uint64()),
                    pa.field("contains_unicode", pa.bool_()),
                ]

            if contents.type == pa.uuid():
                # For UUIDs, we need to convert them to strings for the output.
                min_value = min_value.bytes
                max_value = max_value.bytes

            result_table = pa.Table.from_pylist(
                [
                    {
                        "has_not_null": not_null_count > 0,
                        "has_null": null_count > 0,
                        "distinct_count": distinct_count,
                        "min": min_value,
                        "max": max_value,
                        **additional_values,
                    }
                ],
                schema=pa.schema(
                    [
                        pa.field("has_not_null", pa.bool_()),
                        pa.field("has_null", pa.bool_()),
                        pa.field("distinct_count", pa.uint64()),
                        pa.field("min", contents.type),
                        pa.field("max", contents.type),
                        *additional_schema_fields,
                    ]
                ),
            )
        return result_table

    def impl_do_get(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        ticket: flight.Ticket,
    ) -> flight.RecordBatchStream:
        assert context.caller is not None

        ticket_data = flight_handling.decode_ticket_model(ticket, FlightTicketData)

        descriptor_parts = descriptor_unpack_(ticket_data.descriptor)
        with DatabaseLibraryContext(context.caller.token.token, readonly=True) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)

            if descriptor_parts.type == "table":
                table = schema.by_name("table", descriptor_parts.name)

                if ticket_data.at_unit == "VERSION":
                    assert ticket_data.at_value is not None

                    # Check if at_value is an integer but currently is a string.
                    if not re.match(r"^\d+$", ticket_data.at_value):
                        raise flight.FlightServerError(f"Invalid version: {ticket_data.at_value}")

                    table_version = table.version(int(ticket_data.at_value))
                elif ticket_data.at_unit == "TIMESTAMP":
                    raise flight.FlightServerError("Timestamp not supported for table versioning")
                else:
                    table_version = table.version()

                if descriptor_parts.schema_name == "test_predicate_pushdown" and ticket_data.where_clause is not None:
                    # We are going to do the predicate pushdown for filtering the data we have in memory.
                    # At this point if we have JSON filters we should test that we can decode them.
                    with duckdb.connect(":memory:") as connection:
                        connection.execute("SET TimeZone = 'UTC'")
                        sql = f"select * from table_version where {ticket_data.where_clause}"
                        try:
                            results = connection.execute(sql).fetch_arrow_table()
                        except Exception as e:
                            raise flight.FlightServerError(
                                f"Failed to execute predicate pushdown: {e} sql: {sql}"
                            ) from e
                        table_version = results

                return flight.RecordBatchStream(table_version)
            elif descriptor_parts.type == "table_function":
                table_function = schema.by_name("table_function", descriptor_parts.name)

                output_schema = table_function.output_schema(
                    ticket_data.table_function_parameters,
                    ticket_data.table_function_input_schema,
                )

                return flight.GeneratorStream(
                    output_schema,
                    table_function.handler(
                        parameter_types.TableFunctionParameters(
                            parameters=ticket_data.table_function_parameters, where_clause=ticket_data.where_clause
                        ),
                        output_schema,
                    ),
                )
            else:
                raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")

    def action_table_function_flight_info(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.TableFunctionFlightInfo,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            descriptor_parts = descriptor_unpack_(parameters.descriptor)

            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)

            # Now to get the table function, its a bit harder since they are named by action.
            table_function = schema.by_name("table_function", descriptor_parts.name)

        return table_function.flight_info(
            name=descriptor_parts.name,
            catalog_name=descriptor_parts.catalog_name,
            schema_name=descriptor_parts.schema_name,
            # Pass the real parameters here.
            parameters=parameters,
        )[0]

    def action_flight_info(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        parameters: parameter_types.FlightInfo,
    ) -> flight.FlightInfo:
        assert context.caller is not None

        with DatabaseLibraryContext(context.caller.token.token) as library:
            descriptor_parts = descriptor_unpack_(parameters.descriptor)

            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)

            if descriptor_parts.type == "table_function":
                raise flight.FlightServerError("Table function flight info not supported")
            elif descriptor_parts.type == "table":
                table = schema.by_name("table", descriptor_parts.name)

                version_id = None
                if parameters.at_unit is not None:
                    if parameters.at_unit == "VERSION":
                        assert parameters.at_value is not None

                        # Check if at_value is an integer but currently is a string.
                        if not re.match(r"^\d+$", parameters.at_value):
                            raise flight.FlightServerError(f"Invalid version: {parameters.at_value}")

                        version_id = int(parameters.at_value)
                    elif parameters.at_unit == "TIMESTAMP":
                        raise flight.FlightServerError("Timestamp not supported for table versioning")
                    else:
                        raise flight.FlightServerError(f"Unsupported at_unit: {parameters.at_unit}")

                return table.flight_info(
                    name=descriptor_parts.name,
                    catalog_name=descriptor_parts.catalog_name,
                    schema_name=descriptor_parts.schema_name,
                    version=version_id,
                )[0]
            else:
                raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")

    def exchange_scalar_function(
        self,
        *,
        context: base_server.CallContext[auth.Account, auth.AccountToken],
        descriptor: flight.FlightDescriptor,
        reader: flight.MetadataRecordBatchReader,
        writer: flight.MetadataRecordBatchWriter,
    ) -> None:
        assert context.caller is not None

        descriptor_parts = descriptor_unpack_(descriptor)

        if descriptor_parts.type != "scalar_function":
            raise flight.FlightServerError(f"Unsupported descriptor type: {descriptor_parts.type}")

        with DatabaseLibraryContext(context.caller.token.token) as library:
            database = library.by_name(descriptor_parts.catalog_name)
            schema = database.by_name(descriptor_parts.schema_name)
            scalar_function_info = schema.by_name("scalar_function", descriptor_parts.name)

        writer.begin(scalar_function_info.output_schema)

        for chunk in reader:
            if chunk.data is not None:
                new_rows = pa.Table.from_batches([chunk.data])
                assert new_rows.num_rows > 0

                result = scalar_function_info.handler(new_rows)

                writer.write_table(pa.Table.from_arrays([result], schema=scalar_function_info.output_schema))


@click.command()
@click.option(
    "--location",
    type=str,
    default="grpc://127.0.0.1:50312",
    help="The location where the server should listen.",
)
def run(location: str) -> None:
    log.info("Starting server", location=location)

    auth_manager = auth_manager_naive.AuthManagerNaive[auth.Account, auth.AccountToken](
        account_type=auth.Account,
        token_type=auth.AccountToken,
        allow_anonymous_access=False,
    )

    server = InMemoryArrowFlightServer(
        middleware={
            "headers": base_middleware.SaveHeadersMiddlewareFactory(),
            "auth": base_middleware.AuthManagerMiddlewareFactory(auth_manager=auth_manager),
        },
        location=location,
        auth_manager=auth_manager,
    )
    server.serve()
