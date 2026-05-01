"""
DuckDB loader for SPX trading data
"""

import duckdb
from pathlib import Path
import logging

from config import DATA_PATH, DB_PATH

logger = logging.getLogger(__name__)


class SPXDuckDB:
    """Load SPX price and IV data into DuckDB"""
    
    def __init__(self, db_path=None, data_path=None):
        """
        Initialize DuckDB connection
        
        Args:
            db_path: Path to DuckDB database file
            data_path: Path to parquet data files
        """
        self.db_path = db_path or DB_PATH
        self.data_path = Path(data_path or DATA_PATH)
        self.con = duckdb.connect(str(self.db_path))
        
        logger.info(f"DuckDB initialized: {self.db_path}")
        logger.info(f"Data path: {self.data_path}")
    
    def create_view(self):
        """
        Create DuckDB views for price and IV data
        
        Views created:
        - spx_price_data: SPX 5-minute price data
        - spx_iv_data: SPX implied volatility data
        """
        logger.info("Creating DuckDB views...")
        
        # Create price data view (format: spx_5mins_YYYY-MM-DD.parquet)
        price_pattern = str(self.data_path / "spx_5mins_*.parquet")
        logger.info(f"Price pattern: {price_pattern}")
        
        try:
            self.con.execute(f"""
            CREATE OR REPLACE VIEW spx_price_data AS
            SELECT 
                Date,
                Open,
                High,
                Low,
                Close,
                Volume,
                WAP,
                Count,
                BarSize
            FROM read_parquet('{price_pattern}')
            ORDER BY Date
            """)
            logger.info("✓ Created view: spx_price_data")
        except Exception as e:
            logger.warning(f"Could not create price view: {e}")
            logger.warning("Price data may not be available")
        
        # Create IV data view (format: spx_1day_option_implied_volatility_YYYY-MM-DD.parquet)
        iv_pattern = str(self.data_path / "spx_1day_option_implied_volatility_*.parquet")
        logger.info(f"IV pattern: {iv_pattern}")
        
        try:
            self.con.execute(f"""
            CREATE OR REPLACE VIEW spx_iv_data AS
            SELECT 
                Date,
                Open,
                High,
                Low,
                Close,
                Volume,
                WAP,
                Count,
                BarSize
            FROM read_parquet('{iv_pattern}')
            ORDER BY Date
            """)
            logger.info("✓ Created view: spx_iv_data")
        except Exception as e:
            logger.warning(f"Could not create IV view: {e}")
            logger.warning("IV data may not be available")
    
    def query_price_data(self, start_date=None, end_date=None):
        """
        Query SPX price data
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            DataFrame with price data
        """
        try:
            query = "SELECT * FROM spx_price_data"
            
            if start_date and end_date:
                query += f" WHERE Date >= '{start_date}' AND Date <= '{end_date}'"
            elif start_date:
                query += f" WHERE Date >= '{start_date}'"
            elif end_date:
                query += f" WHERE Date <= '{end_date}'"
            
            query += " ORDER BY Date"
            
            logger.info(f"Querying price data: {start_date or 'all'} to {end_date or 'all'}")
            result = self.con.execute(query).fetchdf()
            logger.info(f"✓ Fetched {len(result)} price rows")
            
            return result
        except Exception as e:
            logger.error(f"Error querying price data: {e}")
            return None
    
    def query_iv_data(self, start_date=None, end_date=None):
        """
        Query SPX IV data
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            DataFrame with IV data
        """
        try:
            query = "SELECT * FROM spx_iv_data"
            
            if start_date and end_date:
                query += f" WHERE Date >= '{start_date}' AND Date <= '{end_date}'"
            elif start_date:
                query += f" WHERE Date >= '{start_date}'"
            elif end_date:
                query += f" WHERE Date <= '{end_date}'"
            
            query += " ORDER BY Date"
            
            logger.info(f"Querying IV data: {start_date or 'all'} to {end_date or 'all'}")
            result = self.con.execute(query).fetchdf()
            logger.info(f"✓ Fetched {len(result)} IV rows")
            
            return result
        except Exception as e:
            logger.error(f"Error querying IV data: {e}")
            return None
    
    def list_available_dates(self):
        """List all available trading dates"""
        try:
            price_dates = self.con.execute(
                "SELECT DISTINCT CAST(Date AS DATE) as date FROM spx_price_data ORDER BY date"
            ).fetchdf()
            
            iv_dates = self.con.execute(
                "SELECT DISTINCT CAST(Date AS DATE) as date FROM spx_iv_data ORDER BY date"
            ).fetchdf()
            
            logger.info(f"Price data available for {len(price_dates)} dates")
            logger.info(f"IV data available for {len(iv_dates)} dates")
            
            return {
                "price_dates": price_dates['date'].tolist() if price_dates is not None else [],
                "iv_dates": iv_dates['date'].tolist() if iv_dates is not None else []
            }
        except Exception as e:
            logger.warning(f"Error listing dates: {e}")
            return {"price_dates": [], "iv_dates": []}
    
    def close(self):
        """Close DuckDB connection"""
        self.con.close()
        logger.info("DuckDB connection closed")
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()


# Example usage
if __name__ == "__main__":
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    # Create database and views
    db = SPXDuckDB()
    
    try:
        db.create_view()
        
        # List available dates
        dates = db.list_available_dates()
        print(f"\nPrice dates: {dates['price_dates']}")
        print(f"IV dates: {dates['iv_dates']}")
        
        # Query price data
        price_data = db.query_price_data()
        if price_data is not None:
            print(f"\nPrice data shape: {price_data.shape}")
            print(price_data.head())
        
        # Query IV data
        iv_data = db.query_iv_data()
        if iv_data is not None:
            print(f"\nIV data shape: {iv_data.shape}")
            print(iv_data.head())
    
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        sys.exit(1)
    
    finally:
        db.close()