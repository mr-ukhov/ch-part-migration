import json
import time

import pendulum
import requests
from airflow import DAG
from airflow.hooks.base import BaseHook
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.exceptions import AirflowException
from requests.auth import HTTPBasicAuth
import re


params = {
    'source_conn_id': 'ch_node_1',  # название конекшена к исходной ноде, с которой будем экспортировать партиции
    'source_database': 'default',
    'source_table': 'trips',
    'source_partitions': ['all', 'or', 'partitions_name'],  #201507
    'target_nodes_conn_id': ['ch_node_2', 'ch_node_3'],
    'target_database': 'default',
    'target_table': 'tmp__trips'
}


def execute_query(conn_id, query):
    """
    Функция, которая выполняет SQL запрос в кликхаусе через HTTP протокол
    :param conn_id: конекшен в airflow
    :param query: SQL запрос
    :return: результат запроса
    """
    connection = BaseHook.get_connection(conn_id)
    username = connection.login
    password = connection.password
    url = f'http://{connection.host}:{connection.port}/'
    print('node: ', conn_id)
    print('query:\n', query)
    response = requests.post(url, params={'query': query}, auth=HTTPBasicAuth(username, password), timeout=1800)
    if response.status_code != 200:
        raise AirflowException(response.text)
    return response.text


def check_table_exists(conn_id, full_table_name):
    """
    Функция проверки существования таблицы в кликхаусе
    :param conn_id: конекшен в airflow
    :param full_table_name: название таблицы в формате "БД.ИМЯ_ТАБЛИЦЫ"
    :return: true/false
    """
    database = full_table_name.split('.')[0]
    table = full_table_name.split('.')[1]
    check_table_query = f"""
        SELECT 1 FROM `system`.`tables` WHERE `database` = '{database}' AND name = '{table}'
    """
    exist_result = execute_query(conn_id, check_table_query)
    return exist_result


def check_params(**kwargs):
    """
    Функция(таск) проверки запуска дага с параметрами. Даг должен быть запущен только с параметрами
    """
    dag_conf = kwargs.get('dag_run').conf
    if not dag_conf:
        return None
    return 'create_replicated_table'


def create_replicated_table(**kwargs):
    """
    Функция(таск) создание реплицируемой таблицы, идентичной исходной, на всех требуемых нодах
    """
    dag_conf = kwargs.get('dag_run').conf

    # Получение DDL исходной таблицы
    source_table_name = dag_conf['source_database'] + '.' + dag_conf['source_table']
    query = f'SHOW CREATE TABLE {source_table_name}'
    # unicode-escape превращает текст "\n" в спец.символы разделителя строки
    origin_table_ddl = execute_query(dag_conf['source_conn_id'], query).encode().decode('unicode-escape')

    # Получение DDL для реплицируемой таблицы
    replicated_table_name = dag_conf['source_database'] + '.tmp_repl__' + dag_conf['source_table']
    replicated_table_ddl = (re.sub(  # изменяем исходный DDL
        r"ENGINE\s*=\s*.*$",  # Берём всю строку после "ENGINE ="
        "ENGINE = ReplicatedMergeTree('/clickhouse/{database}/{table}', '%replica%')",  # Заменяем на новый движок
        origin_table_ddl,
        flags=re.IGNORECASE | re.MULTILINE  # Игнорируем регистр + $ учитывает конец строки
    )
    .replace(source_table_name, replicated_table_name)
    .replace('IF NOT EXISTS', '')
    .replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS'))

    kwargs['ti'].xcom_push(key='replicated_table_name', value=replicated_table_name)  # Сохраняем в xcom репл. таблицу
    kwargs['ti'].xcom_push(key='origin_table_ddl', value=origin_table_ddl)  # Сохраняем в xcom ориг. таблицу

    # Проходимся по каждой ноде, включая источник, и создаем на ней реплицируемую таблицу
    replicated_table_nodes: list = [dag_conf['source_conn_id']] + dag_conf['target_nodes_conn_id']  # список нод
    for i, node in enumerate(replicated_table_nodes):  # node это название конекшена к серверу в airflow
        # Делаем замену "макроса" %replica% на порядковый номер ноды для движка ReplicatedMT
        replica_ddl = replicated_table_ddl.replace('%replica%', str(i+1))

        # Если реплицируемая таблица существует, то ее нельзя удалить и сразу же заново создать, потому что
        # таблица не сразу удалится в зукипере, и при ее создании будет ошибка, что такая таблица уже существует.
        # Поэтому если по каким-то причинам таблица всё же существует, то она будет очищаться, а не удаляться.
        if check_table_exists(node, replicated_table_name):
            execute_query(node, f'TRUNCATE TABLE {replicated_table_name}')
        execute_query(node, replica_ddl)


def attach_partitions(**kwargs):
    """
    Функция(таск) присоединения партиций к реплицируемой таблице от исходной
    """
    dag_conf = kwargs.get('dag_run').conf
    replicated_table_name = kwargs['ti'].xcom_pull(key='replicated_table_name', task_ids='create_replicated_table')
    source_db = dag_conf['source_database']
    source_table = dag_conf['source_table']
    source_conn_id = dag_conf['source_conn_id']
    partitions: list = dag_conf['source_partitions']

    # Если нужно перенести всю таблицу(все партиции), тогда идем в system.parts и получаем все партиции таблицы
    if partitions[0].lower() == 'all':
        get_partitions_query = f"""
            SELECT DISTINCT `partition` 
            FROM `system`.parts 
            WHERE `database` = '{source_db}' AND `table` = '{source_table}'
            FORMAT JSONStrings
        """
        source_table_partitions = json.loads(
            execute_query(source_conn_id, get_partitions_query)
        ).get('data')

        partitions = [row.get('partition') for row in source_table_partitions]

    kwargs['ti'].xcom_push(key='partitions', value=partitions)  # Сохраняем в xcom список партиций

    # аттачим каждую партицию к реплицируемой таблице, чтобы они перенеслись(реплицировались) на другие ноды
    for partition in partitions:
        attach_query = f"""
            ALTER TABLE {replicated_table_name} ATTACH PARTITION {partition} FROM `{source_db}`.`{source_table}`
        """
        execute_query(source_conn_id, attach_query)


def check_replication_status(**kwargs):
    """
    Функция(таск) проверки завершения репликации между нодами
    """
    dag_conf = kwargs.get('dag_run').conf
    replicated_table_name = kwargs['ti'].xcom_pull(key='replicated_table_name', task_ids='create_replicated_table')
    replicated_db = replicated_table_name.split('.')[0]
    replicated_tbl = replicated_table_name.split('.')[1]
    source_conn_id = dag_conf['source_conn_id']
    check_status_query = f"""
        SELECT queue_size
        FROM system.replicas 
        WHERE 
            `database` = '{replicated_db}' AND
            `table` = '{replicated_tbl}'
    """

    while True:  # цикл проверки состояния репликации
        time.sleep(60)
        # тут возвращается строка, поэтому первого символа достаточно, чтобы не попали различные служебные символы
        replication_status = execute_query(source_conn_id, check_status_query)[0]
        if str(replication_status) == '0':  # если статус равен нулю, это значит что репликация таблицы завершилась
            break  # выходим из цикла проверки состояния репликации


def create_target_table(**kwargs):
    """
    Функция(таск) создания целевой таблицы на нодах, присоединения к ней партиций от реплицируемых таблиц и
    удаления реплицируемых таблиц
    """
    dag_conf = kwargs.get('dag_run').conf
    replicated_table_name = kwargs['ti'].xcom_pull(key='replicated_table_name', task_ids='create_replicated_table')
    origin_table_ddl: str = kwargs['ti'].xcom_pull(key='origin_table_ddl', task_ids='create_replicated_table')
    partitions: list = kwargs['ti'].xcom_pull(key='partitions', task_ids='attach_partitions')
    source_table_name: str = dag_conf['source_database'] + '.' + dag_conf['source_table']
    target_table_name: str = dag_conf['target_database'] + '.' + dag_conf['target_table']

    target_table_ddl = origin_table_ddl.replace(source_table_name, target_table_name)

    # на каждой целевой ноде создаем целевую таблицу и аттачим в нее партиции из реплицируемой таблицы
    for node in dag_conf['target_nodes_conn_id']:
        execute_query(node, f"DROP TABLE IF EXISTS {target_table_name}")
        execute_query(node, target_table_ddl)

        for partition in partitions:  # аттачим партиции из реплики в целевую таблицу
            execute_query(
                conn_id=node,
                query=f"ALTER TABLE {target_table_name} ATTACH PARTITION {partition} FROM {replicated_table_name}"
            )

    # дропаем реплицируемые таблицы на всех нодах включая источник
    replicated_table_nodes: list = [dag_conf['source_conn_id']] + dag_conf['target_nodes_conn_id']  # список всех нод
    for node in replicated_table_nodes:
        execute_query(
            conn_id=node,
            query=f"DROP TABLE IF EXISTS {replicated_table_name}"
        )


# Определяем параметры DAG
with DAG(
    dag_id='migrator',
    default_args={
        'owner': 'vukhov',
        'depends_on_past': False,  # зависимость от успешности предыдущего запуска
        'retries': 0,  # кол-во повторных попыток при неудаче
    },
    tags=['ch', 'part_migrator', 'tools'],
    start_date=pendulum.datetime(2025, 5, 4, tz="Europe/Moscow"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    description='Мигратор отдельных партиций ClickHouse между нодами кластера',
    params=params

) as dag:
    op_check_params = BranchPythonOperator(
        task_id='check_params',
        python_callable=check_params
    )

    op_create_replicated_table = PythonOperator(
        task_id='create_replicated_table',
        python_callable=create_replicated_table
    )

    op_attach_partitions = PythonOperator(
        task_id='attach_partitions',
        python_callable=attach_partitions
    )

    op_check_replication_status = PythonOperator(
        task_id='check_replication_status',
        python_callable=check_replication_status
    )

    op_create_target_table = PythonOperator(
        task_id='create_target_table',
        python_callable=create_target_table
    )

    op_check_params >> op_create_replicated_table >> op_attach_partitions
    op_attach_partitions >> op_check_replication_status >> op_create_target_table

