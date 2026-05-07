from pymongo import MongoClient
import json
import os
import certifi

# Load URI from environment if available, or fallback to dev default. 
# It's better to load from config but this is a scratch script.
from chatbot.storage import get_db

db = get_db()

def explain_query(collection_name, query, description, sort=None):
    print(f"\n{'='*50}")
    print(f"--- EXPLAIN: {description} ---")
    print(f"Query: {json.dumps(query)}")
    if sort: print(f"Sort: {sort}")
    print(f"{'='*50}")
    try:
        cursor = db[collection_name].find(query)
        if sort: cursor = cursor.sort(sort)
        
        explanation = cursor.explain()
        
        exec_stats = explanation.get('executionStats', {})
        query_planner = explanation.get('queryPlanner', {})
        winning_plan = query_planner.get('winningPlan', {})
        
        print(f"Execution Time: {exec_stats.get('executionTimeMillis')} ms")
        print(f"Total Docs Examined: {exec_stats.get('totalDocsExamined')}")
        print(f"Total Keys Examined: {exec_stats.get('totalKeysExamined')}")
        print(f"Returned Docs: {exec_stats.get('nReturned')}")
        
        # Determine if COLLSCAN or IXSCAN
        stage = winning_plan.get('stage')
        if stage == 'FETCH' or stage == 'PROJECTION_SIMPLE':
            input_stage = winning_plan.get('inputStage', {})
            stage = input_stage.get('stage')
            index_name = input_stage.get('indexName', 'Unknown')
            if stage == 'FETCH':
                # Sometimes it's nested deeper
                stage = input_stage.get('inputStage', {}).get('stage', stage)
                index_name = input_stage.get('inputStage', {}).get('indexName', index_name)
            print(f"Stage: {stage} (using index: {index_name}) [OK]")
        elif stage == 'COLLSCAN':
            print(f"Stage: {stage} (COLLSCAN = BAD!) [BAD]")
        else:
            print(f"Stage: {stage}")
            
    except Exception as e:
        print(f"Error explaining query: {e}")

# Example query 1: CRM 'Sin Atender'
q1 = {'pipeline_stage': {'$in': ['NEW', None, 'nuevo', 'new']}, 'ejecutivo_asignado': {'$nin': [None, '', 'Sin Asignar', 'No asignado', 'No Asignado', 'Sin asignar'], '$exists': True}}
explain_query('leads', q1, 'KPI: Sin Atender (Asignado pero NEW)')

# Example query 2: CRM 'Sin Asignar'
q2 = {'pipeline_stage': {'$in': ['NEW', None, 'nuevo', 'new']}, '$or': [{'ejecutivo_asignado': {'$in': [None, '', 'Sin Asignar', 'No asignado', 'No Asignado', 'Sin asignar']}}, {'ejecutivo_asignado': {'$exists': False}}]}
explain_query('leads', q2, 'KPI: Sin Asignar (NEW y sin ejecutivo)')

# Example query 3: CRM Pagination
explain_query('leads', {}, 'Pagination: Listar con sort', sort=[("created_at", -1)])
