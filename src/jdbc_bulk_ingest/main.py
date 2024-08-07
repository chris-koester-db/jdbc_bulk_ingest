from databricks.connect.session import DatabricksSession
from pyspark.sql import SparkSession
import textwrap

# Create a new Databricks Connect session. If this fails,
# check that you have configured Databricks Connect correctly.
# See https://docs.databricks.com/dev-tools/databricks-connect.html.
def get_spark() -> SparkSession:
    try:
        return DatabricksSession.builder.getOrCreate()
    except ImportError:
        return SparkSession.builder.getOrCreate() # type: ignore

spark = get_spark()

def _get_partition_boundaries(catalog, schema, table, partition_col) -> tuple[int, int]:
    """Get partition boundaries (Min and max values for partition column)
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
        partition_col (str): Column used to partition queries
    
    Returns:
        tuple[int, int]: Partition boundaries
    """

    partition_boundaries_qry = f"""select
    min({partition_col}) as lower_bound,
    max({partition_col}) as upper_bound
    from {catalog}.{schema}.{table}"""
    
    partition_boundaries_df = spark.sql(partition_boundaries_qry)
    lower_bound = partition_boundaries_df.collect()[0]["lower_bound"]
    upper_bound = partition_boundaries_df.collect()[0]["upper_bound"]
    print(f'lower_bound: {lower_bound}')
    print(f'upper_bound: {upper_bound}')
    
    return lower_bound, upper_bound

def get_partition_spec_delta(catalog, schema, table, partition_col, partition_size_mb) -> dict:
    """Get partition specifications for Delta Lake
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        dict: Partition specifications for Delta Lake
    """
    
    # Get table size
    table_size_in_bytes = spark.sql(f'desc detail {catalog}.{schema}.{table}').collect()[0]['sizeInBytes']
    table_size_mb = table_size_in_bytes / 1024 / 1024
    print(f'table_size_mb: {table_size_mb}')
    
    # Get partition boundaries
    lower_bound, upper_bound = _get_partition_boundaries(catalog, schema, table, partition_col)

    # Get number of partitions
    num_partitions = int(table_size_mb / partition_size_mb)

    partition_spec = {
        'lower_bound': lower_bound,
        'upper_bound': upper_bound,
        'num_partitions': num_partitions
    }

    return partition_spec

def get_partition_spec_sqlserver(catalog, schema, table, partition_col, partition_size_mb) -> dict:
    """Get partition specifications for SQL Server
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        dict: Partition specifications for SQL Server
    """
    
    # Get table size
    spark.sql(f'use catalog {catalog}')
    table_size_qry = f"""\
    SELECT
        CAST(ROUND((SUM(a.total_pages) * 8) / 1024, 2) AS NUMERIC(36, 2)) AS Total_MB
    FROM
        sys.tables t
        JOIN sys.indexes i ON t.OBJECT_ID = i.object_id
        JOIN sys.partitions p ON i.object_id = p.OBJECT_ID AND i.index_id = p.index_id
        JOIN sys.allocation_units a ON p.partition_id = a.container_id
        LEFT OUTER JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE
        s.Name = '{schema}'
        AND t.name = '{table}'
        AND t.is_ms_shipped = 0
        AND i.object_id > 255
    GROUP BY
        t.Name, s.Name, p.Rows
    """
    print(f'table_size_qry:\n{textwrap.dedent(table_size_qry)}')
    table_size = spark.sql(table_size_qry).collect()[0][0]
    print(f'table_size_mb: {table_size}')
    
    # Get partition boundaries
    lower_bound, upper_bound = _get_partition_boundaries(catalog, schema, table, partition_col)

    # Get number of partitions
    num_partitions = int(table_size / partition_size_mb)

    partition_spec = {
        'lower_bound': lower_bound,
        'upper_bound': upper_bound,
        'num_partitions': num_partitions
    }

    return partition_spec

def generate_partition_queries(catalog, schema, table, partition_column, lower_bound:int, upper_bound:int, num_partitions:int) -> list[dict]:
    """Generate list of queries based on partitioning schematic
    
    Function is derived from the JDBC partitioning code in Spark:
    
    Given a partitioning schematic (a column of integral type, a number of
    partitions, and upper and lower bounds on the column's value), generate
    WHERE clauses for each partition so that each row in the table appears
    exactly once.  The parameters minValue and maxValue are advisory in that
    incorrect values may cause the partitioning to be poor, but no data
    will fail to be represented.
    
    Null value predicate is added to the first partition where clause to include
    the rows with null value for the partitions column.
    
    Spark source code - https://github.com/apache/spark/blob/7bbcbb84c266b6ff418cd2c3361aa7350299d0ae/sql/core/src/main/scala/org/apache/spark/sql/execution/datasources/jdbc/JDBCRelation.scala#L129
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
        partition_column (str): Column used to generate partition queries. Using a primary key column is recommended.
        lower_bound (str): Lowest partition column value
        upper_bound (str): Higest partition column value
        num_partitions (str): Number of partition queries
    
    Returns:
        list[dict]: List of dicts containing lower_bound, upper_bound, and src_query
    """

    queryList = []
    stride = int(upper_bound / num_partitions - lower_bound / num_partitions)
    
    i = 0
    currentValue = lower_bound
    while (i < num_partitions):
        lBoundValue = str(currentValue)
        lBound = f'{partition_column} >= {lBoundValue}' if i != 0 else None
        currentValue += stride
        uBoundValue = str(currentValue)
        uBound = f'{partition_column} < {uBoundValue}' if i != num_partitions - 1 else None
        if uBound == None:
            whereClause = lBound
        elif lBound == None:
            whereClause = f'{uBound} or {partition_column} is null'
        else:
            whereClause = f'{lBound} and {uBound}'
        queryList.append({'lower_bound' : int(lBoundValue), 'upper_bound' : int(uBoundValue) - 1, 'src_query' : f'select * from {catalog}.{schema}.{table} where {whereClause}'})
        i = i + 1
        
    return queryList