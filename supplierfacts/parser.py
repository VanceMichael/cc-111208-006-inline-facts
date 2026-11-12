import xml.etree.ElementTree as ET

XBRLI = '{http://www.xbrl.org/2003/instance}'


def inspect(payload):
    root = ET.fromstring(payload)
    contexts = []
    for context in root.iter(XBRLI + 'context'):
        contexts.append({'id': context.get('id'),
                         'entity': context.findtext('.//' + XBRLI + 'identifier'),
                         'instant': context.findtext('.//' + XBRLI + 'instant'),
                         'start': context.findtext('.//' + XBRLI + 'startDate'),
                         'end': context.findtext('.//' + XBRLI + 'endDate')})
    units = [{'id': node.get('id'), 'measures': [measure.text for measure in node.iter(XBRLI + 'measure')]}
             for node in root.iter(XBRLI + 'unit')]
    return {'contexts': contexts, 'units': units}
