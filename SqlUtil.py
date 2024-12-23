import json
import time
import pandas as pd
import pymysql
import datetime

def getDB():
    db = pymysql.connect(host='localhost',user='root',password='root',database='pdd')
    return db
def execute(sql):
    db = getDB()
    cursor = db.cursor()
    try:
        # 执行SQL语句
        cursor.execute(sql)
        # 获取所有记录列表
        results = cursor.fetchall()
        return results
    except Exception as e:
        print(sql)
        print(e)
    finally:
        # 关闭数据库连接
        db.close()
def insert(sql):
    db = getDB()
    cursor = db.cursor()
    try:
        # 执行SQL语句
        cursor.execute(sql)
        # 获取所有记录列表
        db.commit()
        return cursor.lastrowid
    except Exception as e:
        print(sql)
        print(e)
    finally:
        # 关闭数据库连接
        db.close()