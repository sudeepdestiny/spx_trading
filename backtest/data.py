import duckdb
import config


DATA_PATH = config.DATA_PATH

df = duckdb.sql(f"""
    SELECT *
    FROM '{DATA_PATH}/trades_2024-01-02.parquet'
    LIMIT 5
""").df()

print(df)

