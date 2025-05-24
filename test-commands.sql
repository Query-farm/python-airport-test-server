 CREATE SECRET airport_testing (type airport,
 auth_token 'example_token',
 scope 'grpc://localhost:50003/');

CALL airport_action('grpc://localhost:50003/', 'reset');

CALL airport_action('grpc://localhost:50003/', 'create_database', 'test1');

ATTACH 'test1' (TYPE  AIRPORT, location 'grpc://localhost:50003/');



-- So before the functions can be called, we need to load the schema
-- but the schemas are only loaded upon first access.
--
-- there also seems to be kind of a race condition here as the first query
-- doesn't return a result but th esecond one does.

--select * from duckdb_functions() where function_name = 'test_scalar_function';



--show all tables;

CREATE SCHEMA test1.test_schema;

--show all tables;

-- select * from duckdb_schemas();

DROP SCHEMA test1.test_schema;

CREATE SCHEMA test1.test_schema;

use test1.test_schema;

CREATE TABLE employees (
    name STRING,
    age INT
);

--show all tables;

-- SELECT * FROM employees;

INSERT INTO employees (name, age) VALUES ('John Doe', 30);

SELECT * FROM employees;

INSERT INTO employees (name, age) VALUES ('Jane Smith', 25);

SELECT sum(age) FROM employees;

DROP TABLE employees;

CREATE TABLE employees (
    name STRING,
    age INT
);

INSERT INTO employees (name, age) VALUES ('John Doe', 30),
('Jane Smith', 25);

SELECT rowid, name, age FROM employees;

CREATE TABLE employees_2 (
    name STRING,
    age INT
);

INSERT INTO employees_2 (name, age) VALUES ('Alice Johnson', 28),
('Bob Brown', 35);

SELECT * FROM employees_2;

SELECT * FROM employees union select * from employees_2;

SELECT count(*) FROM employees, employees_2;

INSERT INTO employees (name, age)
SELECT name, age FROM employees_2;

SELECT count(*) FROM employees;

DROP TABLE employees_2;

SELECT count(*) FROM employees;

DELETE FROM employees;

SELECT count(*) FROM employees;

INSERT INTO employees (name, age) VALUES
('John Doe', 30),
('Jane Smith', 25) RETURNING name, age;

DELETE FROM employees
WHERE name = 'John Doe' RETURNING name, age;

SELECT * FROM employees;

INSERT INTO employees (name, age) VALUES
('John Doe', 30);

UPDATE employees
set name = 'Emily Davis', age=102 where name = 'Jane Smith';

SELECT * FROM employees;

DELETE FROM employees;

INSERT INTO employees (name, age) VALUES
('John Doe', 30);


UPDATE employees
set name = 'Emily Davis', age=102 where name = 'John Doe'
RETURNING age, name;

UPDATE employees
set name = 'Jen Kelly'
RETURNING age, name;

SELECT * FROM employees;

show all tables;

ALTER TABLE employees
ADD COLUMN location STRING;

SELECT * FROM employees;

update employees set location = 'NYC';


SELECT * FROM employees;

ALTER TABLE employees
DROP COLUMN location;

SELECT * FROM employees;

ALTER TABLE employees
ADD COLUMN location STRING;

update employees set location = 'NYC';

ALTER TABLE employees
RENAME COLUMN location TO city;


SELECT * FROM employees;

ALTER TABLE employees RENAME TO workers;

show all tables;

SELECT * FROM workers;

ALTER TABLE workers
ALTER COLUMN city SET DEFAULT 'Princeton';

show all tables;

insert into workers (name, age, city) values ('John Doe', 30, 'NYC');
insert into workers (name, age) values ('Elliot', 30);

select * from workers;

ALTER TABLE workers
ALTER age TYPE BIGINT;

select * from workers;

ALTER TABLE workers
ALTER age set not null;

show all tables;

insert into workers (name, age) values ('Wilbur', 23);

select * from workers;

update workers set age = age + 1;

select * from workers;

ALTER TABLE workers
ALTER age drop not null;

update workers set age = null where age = 31;

select * from workers;

--ALTER TABLE workers
--ALTER age ADD constraint age > 30;


-- Not yet supported by Airport
-- ALTER TABLE workers
-- ADD primary key (name);


ALTER TABLE workers
add column address STRUCT(
    street TEXT
);

--ALTER TABLE workers
--ADD column address.zip TEXT;

-- update workers set address = {'street': 'value1'};

insert into workers (name, age, city, address) values ('Mary Brown', 30, 'NYC', {'street': 'value1'});

select * from workers;

set arrow_lossless_conversion = true;

create table products (
    description text,
    id uuid,
    counter hugeint
);

insert into products (description, id) values ('product1', '123e4567-e89b-12d3-a456-426614174000');

update products set counter = 1234567890123456789;
select * from products;

select * from PRODUCTS;

-- pyarrow doesn't support unions well
-- CREATE TABLE tbl1 (u UNION(num INTEGER, str VARCHAR));
-- INSERT INTO tbl1 VALUES (1), ('two'), (union_value(str := 'three'));
-- select * from tbl1;


--ALTER TABLE workers add primary key (name);

-- CREATE INDEX workers_name_idx on workers (name);

-- Attach the database again with a different name
ATTACH 'test1' as test2 (TYPE  AIRPORT, location 'grpc://localhost:50003/');


-- Test that a database attached using an alias passes the original
-- name to the server.
SELECT * FROM test2.test_schema.workers;

-- Test that the extension uses the proper database name even if it has been
-- aliased.
CREATE TABLE test2.test_schema.factories(
    name STRING,
    city text
);

INSERT INTO test2.test_schema.factories (name, city) VALUES ('Factory1', 'NYC');

SELECT * from test2.test_schema.factories;

ALTER TABLE test2.test_schema.factories ADD COLUMN value double;

DROP TABLE test2.test_schema.factories;

DELETE from test2.test_schema.workers where age = 103
RETURNING name;

-- Test some scalar functions.
select test1.utils.test_uppercase('test string');

select test1.utils.test_add(5, 6);

select test1.utils.test_add(t.j, 10) from (select unnest([v for v in range(50)]) j) t;

-- test scalar functions that take ANY as a type parameter.
select test1.utils.test_any_type(true);

select test1.utils.test_any_type('hello world');


-- test some table returning functions.

select * from test1.utils.test_echo('hello world2');

select * from test1.utils.test_repeat('hello world with repetition', 10);

select * from test1.utils.test_dynamic_schema('hello world with dynamic schema');

select * from test1.utils.test_dynamic_schema(now());

select * from test1.utils.test_dynamic_schema(55.07224);

select * from test1.utils.test_dynamic_schema(struct_pack(key1 := 'value1', key2 := 42));

-- Test with the named parameters
select * from test1.utils.test_dynamic_schema_named_parameters('Elliott', 555.2222, 'Princeton', location='Main Street');


select * from workers;

select * from test1.utils.test_table_in_out('Sloane', (select name from workers));

select name from test1.utils.test_table_in_out('Sloane', (select name from workers)) order by name;


select result::int from test1.utils.test_repeat('123', 10);

-- test selecting all columns from a wide table

select * from test1.utils.test_wide(10);
select result_3, result_5+result_6 from test1.utils.test_wide(10)
where result_6 > 5;

select * from test1.utils.test_table_in_out_wide('hello', (select unnest(range(10))));

--select result_3, result_6 from test1.utils.test_table_in_out_wide('hello', (select unnest(range(10))));

create table test1.test_schema.wide(
    result_1 int,
    result_2 int,
    result_3 int,
    result_4 int,
    result_5 int,
    result_6 int,
    result_7 int,
    result_8 int,
    result_9 int,
    result_10 int,
    result_11 int,
    result_12 int,
    result_13 int,
    result_14 int,
    result_15 int,
    result_16 int,
    result_17 int,
    result_18 int,
    result_19 int
);

insert into test1.test_schema.wide (
    select
    1,2,3,4,5,6,7,8,9,10,
    11,12,13,14,15,16,17,18,19
    from unnest(range(20)));

select * from test1.test_schema.wide;

create table test1.test_schema.wide_copy as select * from test1.test_schema.wide;


select * from test1.test_schema.wide_copy;

select result_8, result_19 from test1.test_schema.wide;


select result_8, result_19 from test1.utils.test_table_in_out_wide('hello', (select * from test1.test_schema.wide));

-- Test time travel.

create table test1.test_schema.version_test (name text);

insert into test1.test_schema.version_test (name) values ('Elliott');

insert into test1.test_schema.version_test (name) values ('Scarlett');


select * from test1.test_schema.version_test AT (VERSION => 0);

select * from test1.test_schema.version_test AT (VERSION => 2);

select * from test1.test_schema.version_test AT (VERSION => 1);

-- Add a new column but check that we can still time travel around.
alter table test1.test_schema.version_test add column age int;

select * from test1.test_schema.version_test AT (VERSION => 3);

select * from test1.test_schema.version_test AT (VERSION => 2);

--select result_8, result_19 from test1.utils.test_table_in_out_wide('hello', (select unnest as range from unnest(range(10))));
-- fix predicate pushdwon on table in out functions.


-- test statistics on a table

-- test statistics on a table function

-- test cardinaliaty estimation on tabels
-- test cardinality estimation on table functions

