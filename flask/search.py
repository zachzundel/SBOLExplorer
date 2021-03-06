from elasticsearch_dsl import Search
from xml.etree import ElementTree
import re
import requests
import utils
import query


def search_es(es_query):
    body = {
        'query': {
            'function_score': {
                'query': {
                    'multi_match': {
                        'query': es_query,
                        'fields': [
                            'subject',
                            'displayId^3',
                            'version',
                            'name',
                            'description',
                            'type',
                            'keywords'
                        ],
                        'operator': 'or',
                        'fuzziness': 'AUTO',
                    }
                },
                'script_score': {
                    'script': {
                        'source': "_score * Math.log(doc['pagerank'].value + 1)" # Math.log is a natural log
                    }
                }
            }
        },
        'from': 0,
        'size': 10000
    }
    return utils.get_es().search(index=utils.get_config()['elasticsearch_index_name'], body=body)


def empty_search_es(offset, limit, allowed_graphs):
    if len(allowed_graphs) == 1:
        query = { 'term': { 'graph': allowed_graphs[0] } }
    else:
        query = { 'terms': { 'graph': allowed_graphs } }

    body = {
        'query': {
            'function_score': {
                'query': query,
                'script_score': {
                    'script': {
                        'source': "_score * Math.log(doc['pagerank'].value + 1)" # Math.log is a natural log
                    }
                }
            }
        },
        'from': offset,
        'size': limit
    }
    return utils.get_es().search(index=utils.get_config()['elasticsearch_index_name'], body=body)


def extract_query(sparql_query):
    _from = ''
    if is_count_query(sparql_query):
        _from_search = re.search(r'''SELECT \(count\(distinct \?subject\) as \?tempcount\)\s*(.*)\s*WHERE {''', sparql_query)
    else:
        _from_search = re.search(r'''\?type\n(.*)\s*WHERE {''', sparql_query)
    if _from_search:
        _from = _from_search.group(1).strip()

    criteria = ''
    criteria_search = re.search(r'''WHERE {\s*(.*)\s*\?subject a \?type \.''', sparql_query)
    if criteria_search:
        criteria = criteria_search.group(1).strip()

    offset = 0
    offset_search = re.search(r'''OFFSET (\d*)''', sparql_query)
    if offset_search:
        offset = int(offset_search.group(1))

    limit = 50
    limit_search = re.search(r'''LIMIT (\d*)''', sparql_query)
    if limit_search:
        limit = int(limit_search.group(1))

    extract_keyword_re = re.compile(r'''CONTAINS\(lcase\(\?displayId\), lcase\('([^']*)'\)\)''')
    keywords = []
    for keyword in re.findall(extract_keyword_re, criteria):
        keywords.append(keyword)
    es_query = ' '.join(keywords).strip()

    return es_query, _from, criteria, offset, limit


def extract_allowed_graphs(_from):
    allowed_graphs = []

    if utils.get_config()['distributed_search']:
        instances = requests.get('https://wor.synbiohub.org/instances/').json()
        for instance in instances:
            allowed_graphs.append(instance['instanceUrl'] + '/public')

    if _from == '':
        allowed_graphs.append(utils.get_config()['synbiohub_public_graph'])
        return allowed_graphs
    else:
        for graph in _from.split('FROM'):
            graph = graph.strip()
            graph = graph[1:len(graph) - 1]

            if graph != '':
                allowed_graphs.append(graph)

        return allowed_graphs


def is_count_query(sparql_query):
    return 'SELECT (count(distinct' in sparql_query


def create_response(sparql_query, count, bindings):
    if is_count_query(sparql_query):
        response = {"head":{"link":[],"vars":["count"]},"results":{"distinct":False,"ordered":True,"bindings":[{"count":{"type":"typed-literal","datatype":"http://www.w3.org/2001/XMLSchema#integer","value":"10"}}]}}
        response['results']['bindings'][0]['count']['value'] = str(count)
    else:
        response = {"head":{"link":[],"vars":["subject","displayId","version","name","description","type"]},"results":{"distinct":False,"ordered":True,"bindings":[]}}
        response['results']['bindings'] = bindings

    return response


def create_binding(subject, displayId, version, name, description, _type, order_by):
    binding = {}

    if subject is not None:
        binding["subject"] = {
            "type": "uri",
            "datatype": "http://www.w3.org/2001/XMLSchema#uri",
            "value": subject
        }
        
    if displayId is not None:
        binding["displayId"] = {
            "type": "literal",
            "datatype": "http://www.w3.org/2001/XMLSchema#string",
            "value": displayId
        }

    if version is not None:
        binding["version"] = {
            "type": "literal",
            "datatype": "http://www.w3.org/2001/XMLSchema#string",
            "value": version
        }

    if name is not None:
        binding["name"] = {
            "type": "literal",
            "datatype": "http://www.w3.org/2001/XMLSchema#string",
            "value": name
        }

    if description is not None:
        binding["description"] = {
            "type": "literal",
            "datatype": "http://www.w3.org/2001/XMLSchema#string",
            "value": description
        }

    if _type is not None:
        binding["type"] = {
            "type": "uri",
            "datatype": "http://www.w3.org/2001/XMLSchema#uri",
            "value": _type
        }

    if order_by is not None:
        binding["order_by"] = order_by

    return binding


def create_bindings(es_response, clusters, allowed_graphs, allowed_subjects = None):
    bindings = []

    cluster_duplicates = set()

    for hit in es_response['hits']['hits']:
        _source = hit['_source']
        _score = hit['_score']
        subject = _source['subject']

        if allowed_subjects is not None and subject not in allowed_subjects:
            continue

        if _source.get('graph') not in allowed_graphs:
            continue
        
        if subject in cluster_duplicates:
            _score = _score / 2.0
        elif subject in clusters:
            cluster_duplicates.update(clusters[subject])

        if _source['type'] == 'http://sbols.org/v2#Sequence':
            _score = _score / 10.0

        binding = create_binding(subject, 
                _source.get('displayId'),
                _source.get('version'),
                _source.get('name'),
                _source.get('description'),
                _source.get('type'),
                _score)

        bindings.append(binding)

    return bindings


def create_criteria_bindings(criteria_response, uri2rank):
    bindings = []

    for part in criteria_response:
        subject = part['subject']

        if subject not in uri2rank:
            pagerank = 1
        else:
            pagerank = uri2rank[subject]

        if part.get('type') == 'http://sbols.org/v2#Sequence':
            pagerank = pagerank / 10.0

        binding = create_binding(part.get('subject'),
                part.get('displayId'),
                part.get('version'),
                part.get('name'),
                part.get('description'),
                part.get('type'),
                pagerank)

        bindings.append(binding)

    return bindings


def get_allowed_subjects(criteria_response):
    subjects = set()

    for part in criteria_response:
        subjects.add(part['subject'])
    
    return subjects


def create_similar_criteria(criteria, clusters):
    subject = criteria.split(':', 1)[1]

    if subject not in clusters or not clusters[subject]:
        return 'FILTER (?subject != ?subject)'

    return 'FILTER (' + ' || '.join(['?subject = <' + duplicate + '>' for duplicate in clusters[subject]]) + ')'


def search(sparql_query, uri2rank, clusters):
    es_query, _from, criteria, offset, limit = extract_query(sparql_query)

    filterless_criteria = re.sub('FILTER .*', '', criteria).strip()
    allowed_graphs = extract_allowed_graphs(_from)

    if 'SIMILAR' in criteria:
        # SIMILAR
        similar_criteria = create_similar_criteria(criteria, clusters)
        criteria_response = query.query_parts(_from, similar_criteria) 
        bindings = create_criteria_bindings(criteria_response, uri2rank)

    elif 'USES' in criteria or 'TWINS' in criteria or (es_query == '' and filterless_criteria != ''):
        # USES or TWINS or pure advanced search
        criteria_response = query.query_parts(_from, criteria)
        bindings = create_criteria_bindings(criteria_response, uri2rank)

    elif es_query == '' and filterless_criteria == '':
        # empty search
        es_response = empty_search_es(offset, limit, allowed_graphs)
        bindings = create_bindings(es_response, clusters, allowed_graphs)
        bindings.sort(key = lambda binding: binding['order_by'], reverse = True)
        return create_response(sparql_query, es_response['hits']['total'], bindings)

    else:
        es_response = search_es(es_query)

        if filterless_criteria == '':
            # pure string search
            bindings = create_bindings(es_response, clusters, allowed_graphs)

        else:
            # advanced search and string search
            criteria_response = query.query_parts(_from, filterless_criteria)
            allowed_subjects = get_allowed_subjects(criteria_response)
            bindings = create_bindings(es_response, clusters, allowed_graphs, allowed_subjects)

    bindings.sort(key = lambda binding: binding['order_by'], reverse = True)

    return create_response(sparql_query, len(bindings), bindings[offset:offset + limit])

