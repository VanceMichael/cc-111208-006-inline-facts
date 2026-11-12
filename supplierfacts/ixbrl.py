"""Parse the registered iXBRL subset into traceable structured facts.

The parser never guesses: values are produced only through namespace-aware
QName resolution and the two registered numeric transforms.  Anything that
cannot be resolved under docs/source-contract.md is either a hard document
error (the whole revision is rejected) or an isolated fact with a located
diagnostic.
"""
import json
import re
from decimal import Decimal
import xml.etree.ElementTree as ET

XBRLI_NS = 'http://www.xbrl.org/2003/instance'
IX_NS = 'http://www.xbrl.org/2013/inlineXBRL'
XBRDI_NS = 'http://xbrl.org/2006/xbrldi'
XSI_NS = 'http://www.w3.org/2001/XMLSchema-instance'

XBRLI = '{' + XBRLI_NS + '}'
IX = '{' + IX_NS + '}'
XBRDI = '{' + XBRDI_NS + '}'
XSI_NIL = '{' + XSI_NS + '}nil'
CONTEXT = XBRLI + 'context'
UNIT = XBRLI + 'unit'
MEASURE = XBRLI + 'measure'
DIVIDE = XBRLI + 'divide'
NUMERATOR = XBRLI + 'unitNumerator'
DENOMINATOR = XBRLI + 'unitDenominator'
NON_FRACTION = IX + 'nonFraction'
NON_NUMERIC = IX + 'nonNumeric'
CONTINUATION = IX + 'continuation'
EXCLUDE = IX + 'exclude'

# The only two registered transforms, under urn:local:ixt.
REGISTERED_TRANSFORMS = {
    'urn:local:ixt:num-dot-decimal': 'dot',
    'urn:local:ixt:num-comma-decimal': 'comma',
}

# Diagnostic codes (isolated facts and recoverable structural problems).
UNKNOWN_TRANSFORM = 'unknown-transform'
INVALID_NUMBER = 'invalid-number'
INVALID_ATTRIBUTE = 'invalid-attribute'
CONTINUATION_CYCLE = 'continuation-cycle'
CONTINUATION_DANGLING = 'continuation-dangling'
CONTINUATION_DUPLICATE = 'continuation-duplicate-reference'
CONTINUATION_TARGET_KIND = 'continuation-target-kind'

_INTEGER_RE = re.compile(r'^-?\d+$')
_PLAIN_NUMBER_RE = re.compile(r'^-?\d+(?:\.\d+)?$')
_DOT_NUMBER_RE = re.compile(r'^-?\d+(?:,\d{3})*(?:\.\d+)?$')
_COMMA_NUMBER_RE = re.compile(r'^-?\d+(?:\.\d{3})*(?:,\d+)?$')


class DocumentError(ValueError):
    """Hard failure: the document revision cannot be received at all."""


class _NamespaceTreeBuilder(ET.TreeBuilder):
    """TreeBuilder that records the in-scope namespace map for every node."""

    def __init__(self):
        super().__init__()
        self.stack = [{}]
        self.maps = {}

    def start_ns(self, prefix, uri):
        scope = dict(self.stack[-1])
        scope[prefix or ''] = uri
        self.stack.append(scope)

    def end_ns(self, prefix):
        self.stack.pop()

    def start(self, tag, attrs):
        element = super().start(tag, attrs)
        self.maps[id(element)] = dict(self.stack[-1])
        return element


def _qname(text, namespace_map):
    """Expand a prefix:local QName using namespace URIs, never prefix text."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    prefix, _, local = text.partition(':')
    if not local:
        prefix, local = '', prefix
    uri = namespace_map.get(prefix)
    if uri is None:
        return None
    return {'namespace': uri, 'local': local, 'qualified': text}


def _identity_qname(qname):
    """QName identity is namespace URI + local name, never prefix text."""
    return {'namespace': qname['namespace'], 'local': qname['local']}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _identity_canonical(value):
    """Deterministic key built only from namespace URIs and local names."""
    if isinstance(value, dict):
        if set(value) >= {'namespace', 'local'}:
            return _canonical(_identity_qname(value))
        return _canonical({key: _identity_canonical(inner)
                           for key, inner in value.items()})
    if isinstance(value, list):
        return _canonical(sorted(_identity_canonical(item) for item in value))
    return _canonical(value)


def _canonical_decimal(decimal_value):
    text = format(decimal_value, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    if text in ('', '-', '-0'):
        text = '0'
    return text


def parse_number(text, transform):
    """Parse with a registered transform only; return None when not matched."""
    if text is None:
        return None
    token = text.strip()
    if transform is None:
        if not _PLAIN_NUMBER_RE.fullmatch(token):
            return None
        try:
            return Decimal(token)
        except Exception:
            return None
    negative = False
    if len(token) >= 2 and token.startswith('(') and token.endswith(')'):
        negative = True
        token = token[1:-1]
    if transform == 'dot':
        pattern = _DOT_NUMBER_RE
    else:
        pattern = _COMMA_NUMBER_RE
    if not pattern.fullmatch(token):
        return None
    if token.startswith('-'):
        negative = not negative
        token = token[1:]
    if transform == 'dot':
        token = token.replace(',', '')
    else:
        token = token.replace('.', '').replace(',', '.')
    try:
        value = Decimal(token)
    except Exception:
        return None
    return -value if negative else value


def _own_text(element):
    """Concatenate text under a node, excluding every ix:exclude subtree."""
    pieces = []
    if element.text:
        pieces.append(element.text)
    for child in element:
        if child.tag != EXCLUDE:
            pieces.append(_own_text(child))
        if child.tail:
            pieces.append(child.tail)
    return ''.join(pieces)


def extract(payload):
    """Extract facts from one iXBRL document revision.

    Returns a plain dict (contexts, units, facts, isolated facts, diagnostics).
    Raises DocumentError on hard failures; never performs I/O.
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise DocumentError('document payload must be bytes')
    # Defence in depth: expat rejects external entities anyway, but refuse any
    # internal DTD/entity subset so untrusted pages cannot smuggle DTD behaviour.
    head = payload[:4096].lower()
    if b'<!entity' in head:
        raise DocumentError('DTD/entity declarations are not permitted')

    builder = _NamespaceTreeBuilder()
    parser = ET.XMLParser(target=builder)
    try:
        parser.feed(bytes(payload))
        root = parser.close()
    except ET.ParseError as error:
        raise DocumentError('malformed XHTML document: ' + str(error)) from error

    maps = builder.maps

    def nsmap(element):
        return maps.get(id(element), {})

    # Parent map and sibling-aware locators.
    parents = {}
    for parent in root.iter():
        for child in parent:
            parents[id(child)] = parent

    def locator(element):
        if element.get('id'):
            return element.tag.split('}', 1)[-1] + "[@id='" + element.get('id') + "']"
        chain = []
        current = element
        while current is not root:
            parent = parents.get(id(current))
            if parent is None:
                break
            local = current.tag.split('}', 1)[-1]
            same = [node for node in parent if node.tag == current.tag]
            index = same.index(current) + 1 if len(same) > 1 else 1
            chain.append(local + '[' + str(index) + ']')
            current = parent
        chain.reverse()
        return '/' + '/'.join(chain)

    # Single ID space: duplicate IDs must be locatable and reject the revision.
    id_index = {}
    for element in root.iter():
        node_id = element.get('id')
        if node_id is None:
            continue
        if node_id in id_index:
            raise DocumentError(
                "duplicate node id '" + node_id + "' at " + locator(element)
                + ' and ' + locator(id_index[node_id]))
        id_index[node_id] = element

    diagnostics = []

    def add_diagnostic(code, element, reason, **extra):
        record = {'code': code, 'locator': locator(element),
                  'node_id': element.get('id'), 'reason': reason}
        record.update(extra)
        diagnostics.append(record)
        return record

    # ---------------------------------------------------------------- contexts
    contexts = {}
    for context in root.iter(CONTEXT):
        context_id = context.get('id')
        identifier = context.find('.//' + XBRLI + 'identifier')
        period_node = context.find(XBRLI + 'period')
        if identifier is None or identifier.text is None:
            raise DocumentError('context ' + str(context_id) + ' lacks an entity identifier')
        if period_node is None:
            raise DocumentError('context ' + str(context_id) + ' lacks a period')
        period = {}
        instant = period_node.find(XBRLI + 'instant')
        if instant is not None:
            period['instant'] = (instant.text or '').strip()
        else:
            start = period_node.find(XBRLI + 'startDate')
            end = period_node.find(XBRLI + 'endDate')
            if start is None or end is None:
                raise DocumentError('context ' + str(context_id)
                                    + ' period must be instant or duration')
            period = {'start': (start.text or '').strip(),
                     'end': (end.text or '').strip()}
        members = []
        for member in context.iter(XBRDI + 'explicitMember'):
            dimension = _qname(member.get('dimension'), nsmap(member))
            value = _qname(member.text, nsmap(member))
            if dimension is None or value is None:
                raise DocumentError('context ' + str(context_id)
                                    + ' has an unresolvable explicitMember QName')
            members.append({'dimension': dimension, 'member': value})
        members.sort(key=lambda item: _canonical(item))
        canonical = {'entity': {'scheme': identifier.get('scheme'),
                                'value': identifier.text.strip()},
                     'period': period, 'dimensions': members}
        contexts[context_id] = {'id': context_id, 'canonical': canonical,
                                'key': _identity_canonical(canonical)}

    # ------------------------------------------------------------------- units
    units = {}
    for unit in root.iter(UNIT):
        unit_id = unit.get('id')
        divide = unit.find(DIVIDE)
        if divide is not None:
            numerator_node = divide.find(NUMERATOR)
            denominator_node = divide.find(DENOMINATOR)
            if numerator_node is None or denominator_node is None:
                raise DocumentError('unit ' + str(unit_id) + ' has a malformed divide')
            numerator = sorted((_qname(node.text, nsmap(unit))
                                for node in numerator_node.iter(MEASURE)),
                               key=_canonical)
            denominator = sorted((_qname(node.text, nsmap(unit))
                                  for node in denominator_node.iter(MEASURE)),
                                 key=_canonical)
            if not numerator or not denominator:
                raise DocumentError('unit ' + str(unit_id) + ' has empty divide measures')
            if None in numerator or None in denominator:
                raise DocumentError('unit ' + str(unit_id) + ' has an unresolvable measure QName')
            canonical = {'divide': {'numerator': numerator,
                                    'denominator': denominator}}
        else:
            measures = sorted((_qname(node.text, nsmap(unit))
                               for node in unit
                               if node.tag == MEASURE), key=_canonical)
            if not measures:
                raise DocumentError('unit ' + str(unit_id) + ' has no measures')
            if any(measure is None for measure in measures):
                raise DocumentError('unit ' + str(unit_id) + ' has an unresolvable measure QName')
            canonical = {'measures': measures}
        units[unit_id] = {'id': unit_id, 'canonical': canonical,
                          'key': _identity_canonical(canonical)}

    def resolve_ref(element, attribute, expected_tag, label):
        ref = element.get(attribute)
        if not ref:
            raise DocumentError(locator(element) + ' missing ' + attribute)
        target = id_index.get(ref)
        if target is None or target.tag != expected_tag:
            raise DocumentError(locator(element) + ' ' + attribute
                                + " '" + str(ref) + "' does not resolve to a " + label)
        return ref

    # ------------------------------------------------------- continuation chains
    incoming = {}

    def gather_text(start):
        """Follow continuedAt; return (text, diagnostics). None text = unusable."""
        pieces = []
        local_diagnostics = []
        seen = set()
        current = start
        while True:
            pieces.append(_own_text(current))
            ref = current.get('continuedAt')
            if not ref:
                break
            if ref in seen:
                local_diagnostics.append(add_diagnostic(
                    CONTINUATION_CYCLE, current,
                    "continuedAt '" + ref + "' revisits a node already in the chain",
                    chain=sorted(seen | {ref})))
                return None, local_diagnostics
            seen.add(ref)
            target = id_index.get(ref)
            if target is None:
                local_diagnostics.append(add_diagnostic(
                    CONTINUATION_DANGLING, current,
                    "continuedAt '" + ref + "' has no node with that id"))
                return None, local_diagnostics
            if target.tag != CONTINUATION:
                local_diagnostics.append(add_diagnostic(
                    CONTINUATION_TARGET_KIND, target,
                    "continuedAt '" + ref + "' does not point at ix:continuation"))
                return None, local_diagnostics
            incoming[ref] = incoming.get(ref, 0) + 1
            if incoming[ref] > 1:
                local_diagnostics.append(add_diagnostic(
                    CONTINUATION_DUPLICATE, target,
                    "continuation '" + ref + "' is referenced by more than one fact",
                    reference_count=incoming[ref]))
                return None, local_diagnostics
            current = target
        return ''.join(pieces), local_diagnostics

    # ------------------------------------------------------------------- facts
    facts = []
    isolated = []

    def base_record(element, kind, concept, context_id, unit_id):
        return {'kind': kind, 'concept': concept,
                'context_id': context_id,
                'context_key': contexts[context_id]['key'],
                'unit_id': unit_id,
                'unit_key': units[unit_id]['key'] if unit_id else '',
                'node_id': element.get('id'), 'locator': locator(element),
                'raw': (element.text or '').strip() if kind == 'nonFraction'
                else _own_text(element)}

    for element in root.iter():
        if element.tag not in (NON_FRACTION, NON_NUMERIC):
            continue
        kind = 'nonFraction' if element.tag == NON_FRACTION else 'nonNumeric'
        concept = _qname(element.get('name'), nsmap(element))
        if concept is None:
            raise DocumentError(locator(element) + ' has an unresolvable name QName')
        context_id = resolve_ref(element, 'contextRef', CONTEXT, 'context')
        unit_id = None
        if kind == 'nonFraction':
            unit_id = resolve_ref(element, 'unitRef', UNIT, 'unit')

        if kind == 'nonNumeric':
            text, chain_diags = gather_text(element)
            record = base_record(element, kind, concept, context_id, None)
            record.update({'text': text, 'nil': False})
            if chain_diags:
                isolated.append(record)
            else:
                facts.append(record)
            continue

        record = base_record(element, kind, concept, context_id, unit_id)
        record.update({'format': element.get('format'),
                       'decimals': element.get('decimals'),
                       'scale': element.get('scale', '0'),
                       'sign': element.get('sign'),
                       'nil': element.get(XSI_NIL) == 'true'})

        if record['nil']:
            record.update({'text': None, 'value': None})
            facts.append(record)
            continue

        format_qname = None
        transform = None
        if record['format']:
            format_qname = _qname(record['format'], nsmap(element))
            if format_qname is None:
                record['reason'] = "format QName '" + record['format'] + "' cannot be resolved"
                add_diagnostic(UNKNOWN_TRANSFORM, element, record['reason'])
                isolated.append(record)
                continue
            transform = REGISTERED_TRANSFORMS.get(
                format_qname['namespace'] + ':' + format_qname['local'])
            if transform is None:
                record['reason'] = ('transform ' + format_qname['namespace']
                                    + ':' + format_qname['local'] + ' is not registered')
                add_diagnostic(UNKNOWN_TRANSFORM, element, record['reason'],
                               transform=format_qname['namespace'] + ':'
                               + format_qname['local'])
                isolated.append(record)
                continue

        text, chain_diags = gather_text(element)
        if chain_diags:
            isolated.append(record)
            continue
        record['raw'] = text.strip()

        value = parse_number(text, transform)
        if value is None:
            record['reason'] = "text '" + text + "' does not match the " + (
                transform or 'plain decimal') + ' grammar'
            add_diagnostic(INVALID_NUMBER, element, record['reason'])
            isolated.append(record)
            continue

        if not _INTEGER_RE.fullmatch(record['scale']):
            record['reason'] = "scale '" + record['scale'] + "' is not an integer"
            add_diagnostic(INVALID_ATTRIBUTE, element, record['reason'])
            isolated.append(record)
            continue
        decimals = record['decimals']
        if decimals is not None and decimals != 'INF' and not _INTEGER_RE.fullmatch(decimals):
            record['reason'] = "decimals '" + str(decimals) + "' must be an integer or INF"
            add_diagnostic(INVALID_ATTRIBUTE, element, record['reason'])
            isolated.append(record)
            continue
        if record['sign'] not in (None, '-'):
            record['reason'] = "sign '" + str(record['sign']) + "' is supported only as '-'"
            add_diagnostic(INVALID_ATTRIBUTE, element, record['reason'])
            isolated.append(record)
            continue

        # scale then sign, each applied exactly once, all in exact Decimal.
        value = value * (Decimal(10) ** int(record['scale']))
        if record['sign'] == '-':
            value = -value
        record['value'] = _canonical_decimal(value)
        facts.append(record)

    # ---------------------------------------------------------- group facts
    groups = {}
    order = []
    for record in facts:
        key = (record['kind'], record['concept']['namespace'],
               record['concept']['local'], record['context_key'],
               record['unit_key'])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)

    grouped = []
    for key in order:
        members = groups[key]
        first = members[0]
        if first['kind'] == 'nonFraction':
            values = sorted({member['value'] for member in members})
            status = 'merged' if len(values) == 1 else 'conflict'
            sources = [{
                'node_id': member['node_id'], 'locator': member['locator'],
                'raw': member['raw'], 'value': member['value'],
                'decimals': member['decimals'], 'scale': member['scale'],
                'sign': member['sign'], 'format': member['format'],
                'nil': member['nil'],
            } for member in members]
            grouped.append({
                'kind': 'nonFraction', 'concept': first['concept'],
                'context_id': first['context_id'], 'unit_id': first['unit_id'],
                'context_key': first['context_key'], 'unit_key': first['unit_key'],
                'status': status, 'values': values, 'sources': sources,
                'nil': all(member['nil'] for member in members)})
        else:
            texts = sorted({member['text'] for member in members})
            status = 'merged' if len(texts) == 1 else 'conflict'
            sources = [{'node_id': member['node_id'],
                        'locator': member['locator'], 'text': member['text']}
                       for member in members]
            grouped.append({
                'kind': 'nonNumeric', 'concept': first['concept'],
                'context_id': first['context_id'], 'unit_id': None,
                'context_key': first['context_key'], 'unit_key': '',
                'status': status, 'values': texts, 'sources': sources})

    return {
        'contexts': sorted(contexts.values(), key=lambda item: item['id']),
        'units': sorted(units.values(), key=lambda item: item['id']),
        'facts': grouped,
        'isolated': isolated,
        'diagnostics': diagnostics,
    }
