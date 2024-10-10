import pymysql
from sqlalchemy import create_engine

def getConnect():
    config = {
        'host': '47.100.235.90',
        'port':6666,
        'user': 'root',
        'password': 'lujie001122',
        'database': 'quant',
        'charset': 'utf8mb4',
        'cursorclass': pymysql.cursors.DictCursor
    }
    connection = pymysql.connect(**config)
    return connection
def get_engine():
    connection_string = 'mysql+pymysql://root:lujie001122@47.100.235.90:6666/quant'
    engine = create_engine(connection_string)
    return engine

