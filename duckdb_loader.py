import duckdb
from config import DATA_PATH, DB_PATH


class SPXDuckDB:
    def __init__(self, data_path=DATA_PATH, db_path=DB_PATH):
        self.data_path = data_path
        self.db_path = db_path
        self.con = duckdb.connect(str(self.db_path))

    def create_view(self):
        self.con.execute(f"""
            CREATE OR REPLACE VIEW spx_trades AS
            SELECT *
            FROM '{self.data_path}/trades_*.parquet'
        """)

    def sample_file(self, trade_date: str, limit: int = 5):
        return self.con.execute(f"""
            SELECT *
            FROM '{self.data_path}/trades_{trade_date}.parquet'
            LIMIT {limit}
        """).df()

    def sample_view(self, limit: int = 5):
        return self.con.execute(f"""
            SELECT *
            FROM spx_trades
            LIMIT {limit}
        """).df()

    def describe_view(self):
        return self.con.execute("""
            DESCRIBE SELECT * FROM spx_trades
        """).df()

    def daily_counts(self):
        return self.con.execute("""
            SELECT trade_date, COUNT(*) AS row_count
            FROM spx_trades
            GROUP BY trade_date
            ORDER BY trade_date
        """).df()
