import __builtin__, re, sys, threading, types, inspect
from compiler import ast, parse
from operator import attrgetter, itemgetter
from itertools import count, ifilter, ifilterfalse, imap, izip, chain
import datetime

try: from pony.thirdparty import etree
except ImportError: etree = None

from pony import options
from pony.decompiling import decompile
from pony.sqlsymbols import *
from pony.dbapiprovider import (
    LongStr, LongUnicode,
    DBException, RowNotFound, MultipleRowsFound, TooManyRowsFound,
    Warning, Error, InterfaceError, DatabaseError, DataError, OperationalError,
    IntegrityError, InternalError, ProgrammingError, NotSupportedError
    )
from pony.utils import (
    localbase, simple_decorator, decorator_with_params, cut_traceback,
    import_module, parse_expr, is_ident, reraise, avg, tostring
    )

__all__ = '''
    DBException RowNotFound MultipleRowsFound TooManyRowsFound

    Warning Error InterfaceError DatabaseError DataError OperationalError
    IntegrityError InternalError ProgrammingError NotSupportedError

    OrmError ERDiagramError SchemaError MappingError ConstraintError CacheIndexError ObjectNotFound
    MultipleObjectsFoundError TooManyObjectsFoundError OperationWithDeletedObjectError
    TransactionError TransactionIntegrityError IsolationError CommitException RollbackException
    UnrepeatableReadError UnresolvableCyclicDependency UnexpectedError

    Database sql_debug show

    Optional Required Unique PrimaryKey Set
    flush commit rollback with_transaction

    LongStr LongUnicode

    TranslationError
    query fetch fetch_one fetch_count fetch_sum fetch_min fetch_max fetch_avg exists
    avg

    JOIN
    '''.split()

debug = False

def sql_debug(value):
    global debug
    debug = value

class OrmError(Exception): pass

class ERDiagramError(OrmError): pass
class SchemaError(OrmError): pass
class MappingError(OrmError): pass
class ConstraintError(OrmError): pass
class CacheIndexError(OrmError): pass

class ObjectNotFound(OrmError):
    def __init__(exc, entity, pkval):
        if type(pkval) is tuple:
            pkval = ','.join(map(repr, pkval))
        else: pkval = repr(pkval)
        msg = '%s[%s]' % (entity.__name__, pkval)
        OrmError.__init__(exc, msg)
        exc.entity = entity
        exc.pkval = pkval

class MultipleObjectsFoundError(OrmError): pass
class TooManyObjectsFoundError(OrmError): pass
class OperationWithDeletedObjectError(OrmError): pass
class TransactionError(OrmError): pass

class TransactionIntegrityError(TransactionError):
    def __init__(exc, msg, original_exc=None):
        Exception.__init__(exc, msg)
        exc.original_exc = original_exc
        
class CommitException(TransactionError):
    def __init__(exc, msg, exceptions):
        Exception.__init__(exc, msg)
        exc.exceptions = exceptions

class PartialCommitException(TransactionError):
    def __init__(exc, msg, exceptions):
        Exception.__init__(exc, msg)
        exc.exceptions = exceptions

class RollbackException(TransactionError):
    def __init__(exc, msg, exceptions):
        Exception.__init__(exc, msg)
        exc.exceptions = exceptions

class TransactionRolledBack(TransactionError): pass
class IsolationError(TransactionError): pass
class   UnrepeatableReadError(IsolationError): pass
class UnresolvableCyclicDependency(TransactionError): pass

class UnexpectedError(TransactionError):
    def __init__(exc, msg, original_exc):
        Exception.__init__(exc, msg)
        exc.original_exc = original_exc

class TranslationError(Exception): pass

###############################################################################

sql_cache = {}

def adapt_sql(sql, paramstyle):
    result = sql_cache.get((sql, paramstyle))
    if result is not None: return result
    pos = 0
    result = []
    args = []
    keyargs = {}
    if paramstyle in ('format', 'pyformat'): sql = sql.replace('%', '%%')
    while True:
        try: i = sql.index('$', pos)
        except ValueError:
            result.append(sql[pos:])
            break
        result.append(sql[pos:i])
        if sql[i+1] == '$':
            result.append('$')
            pos = i+2
        else:
            try: expr, _ = parse_expr(sql, i+1)
            except ValueError:
                raise # TODO
            pos = i+1 + len(expr)
            if expr.endswith(';'): expr = expr[:-1]
            compile(expr, '<?>', 'eval')  # expr correction check
            if paramstyle == 'qmark':
                args.append(expr)
                result.append('?')
            elif paramstyle == 'format':
                args.append(expr)
                result.append('%s')
            elif paramstyle == 'numeric':
                args.append(expr)
                result.append(':%d' % len(args))
            elif paramstyle == 'named':
                key = 'p%d' % (len(keyargs) + 1)
                keyargs[key] = expr
                result.append(':' + key)
            elif paramstyle == 'pyformat':
                key = 'p%d' % (len(keyargs) + 1)
                keyargs[key] = expr
                result.append('%%(%s)s' % key)
            else: raise NotImplementedError
    adapted_sql = ''.join(result)
    if args:
        source = '(%s,)' % ', '.join(args)
        code = compile(source, '<?>', 'eval')
    elif keyargs:
        source = '{%s}' % ','.join('%r:%s' % item for item in keyargs.items())
        code = compile(source, '<?>', 'eval')
    else:
        code = compile('None', '<?>', 'eval')
        if paramstyle in ('format', 'pyformat'): sql = sql.replace('%%', '%')
    result = adapted_sql, code
    sql_cache[(sql, paramstyle)] = result
    return result

next_num = count().next

class Local(localbase):
    def __init__(local):
        local.db2cache = {}

local = Local()        

select_re = re.compile(r'\s*select\b', re.IGNORECASE)

class Database(object):
    @cut_traceback
    def __init__(self, provider_name, *args, **keyargs):
        # First argument cannot be named 'database', because 'database' can be in keyargs
        if not isinstance(provider_name, basestring): raise TypeError
        provider_module = import_module('pony.dbproviders.' + provider_name)
        self.provider = provider = provider_module.get_provider(*args, **keyargs)
        self.priority = 0
        self.optimistic = True
        self._insert_cache = {}

        # connection test with immediate release:
        connection = provider.connect()
        provider.release(connection)

        # ER-diagram related stuff:
        self.entities = {}
        self._unmapped_attrs = {}
        self.schema = None
        self.Entity = type.__new__(EntityMeta, 'Entity', (Entity,), {})
        self.Entity._database_ = self
    @cut_traceback
    def get_connection(database):
        cache = database._get_cache()
        cache.optimistic = False
        return cache.connection
    def _get_cache(database):
        cache = local.db2cache.get(database)
        if cache is not None: return cache
        connection = database.provider.connect()
        cache = local.db2cache[database] = Cache(database, connection)
        return cache
    @cut_traceback
    def flush(database):
        cache = database._get_cache()
        cache.flush()
    @cut_traceback
    def commit(database):
        cache = local.db2cache.get(database)
        if cache is not None: cache.commit()
    @cut_traceback
    def rollback(database):
        cache = local.db2cache.get(database)
        if cache is not None: cache.rollback()
    @cut_traceback
    def execute(database, sql, globals=None, locals=None):
        database._get_cache().optimistic = False
        return database._execute(sql, globals, locals, 2)
    def _execute(database, sql, globals, locals, frame_depth):
        sql = sql[:]  # sql = templating.plainstr(sql)
        if globals is None:
            assert locals is None
            frame_depth += 1
            globals = sys._getframe(frame_depth).f_globals
            locals = sys._getframe(frame_depth).f_locals
        provider = database.provider
        adapted_sql, code = adapt_sql(sql, provider.paramstyle)
        values = eval(code, globals, locals)
        if values is None: values = ()
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        provider.execute(cursor, adapted_sql, values)
        return cursor
    @cut_traceback
    def select(database, sql, globals=None, locals=None, frame_depth=0):
        if not select_re.match(sql): sql = 'select ' + sql
        cursor = database._execute(sql, globals, locals, frame_depth+2)
        max_fetch_count = options.MAX_FETCH_COUNT
        if max_fetch_count is not None:
            result = cursor.fetchmany(max_fetch_count)
            if cursor.fetchone() is not None: raise TooManyRowsFound
        else: result = cursor.fetchall()
        if len(cursor.description) == 1: result = [ row[0] for row in result ]
        else:
            row_class = type("row", (tuple,), {})
            for i, column_info in enumerate(cursor.description):
                column_name = column_info[0]
                if not is_ident(column_name): continue
                if hasattr(tuple, column_name) and column_name.startswith('__'): continue
                setattr(row_class, column_name, property(itemgetter(i)))
            result = [ row_class(row) for row in result ]
        return result
    @cut_traceback
    def get(database, sql, globals=None, locals=None):
        rows = database.select(sql, globals, locals, 2)
        if not rows: raise RowNotFound
        if len(rows) > 1: raise MultipleRowsFound
        row = rows[0]
        return row
    @cut_traceback
    def exists(database, sql, globals=None, locals=None):
        if not select_re.match(sql): sql = 'select ' + sql
        cursor = database._execute(sql, globals, locals, 2)
        result = cursor.fetchone()
        return bool(result)
    @cut_traceback
    def insert(database, table_name, returning=None, **keyargs):
        table_name = table_name[:]  # table_name = templating.plainstr(table_name)
        cache = database._get_cache()
        cache.optimistic = False
        query_key = (table_name,) + tuple(keyargs)  # keys are not sorted deliberately!!
        if returning is not None: query_key = query_key + (returning,)
        cached_sql = database._insert_cache.get(query_key)
        if cached_sql is None:
            ast = [ INSERT, table_name, keyargs.keys(), [ [PARAM, i] for i in range(len(keyargs)) ], returning ]
            sql, adapter = database._ast2sql(ast)
            cached_sql = sql, adapter
            database._insert_cache[query_key] = cached_sql
        else: sql, adapter = cached_sql
        arguments = adapter(keyargs.values())  # order of values same as order of keys
        cursor = database._exec_sql(sql, arguments)
        if returning is None:
            return getattr(cursor, 'lastrowid', None)
        else:
            return cursor.fetchone()[0]
    def _ast2sql(database, sql_ast):
        sql, adapter = database.provider.ast2sql(sql_ast)
        return sql, adapter
    def _exec_sql(database, sql, arguments=None):
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        if debug:
            print sql
            if arguments: print args2str(arguments)
            print
        if arguments is None: database.provider.execute(cursor, sql)
        else: database.provider.execute(cursor, sql, arguments)
        return cursor
    def _exec_sql_returning_id(database, sql, arguments, returning_py_type):
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        if debug:
            print sql
            if arguments: print args2str(arguments)
            print
        new_id = database.provider.execute_returning_id(cursor, sql, arguments, returning_py_type)
        return new_id
    def _exec_sql_many(database, sql, arguments_list):
        cache = database._get_cache()
        cursor = cache.connection.cursor()
        if debug:
            print 'EXECUTEMANY\n', sql
            for args in arguments_list: print args2str(args)
            print
        database.provider.executemany(cursor, sql, arguments_list)
        return cursor
    @cut_traceback
    def generate_mapping(database, filename=None, check_tables=False, create_tables=False):
        if create_tables and check_tables: raise TypeError(
            "Parameters 'check_tables' and 'create_tables' cannot be set to True at the same time")

        def get_columns(table, column_names):
            return tuple(map(table.column_dict.__getitem__, column_names))

        if database.schema: raise MappingError('Mapping was already generated')
        if filename is not None: raise NotImplementedError
        for entity_name in database._unmapped_attrs:
            raise ERDiagramError('Entity definition %s was not found' % entity_name)

        provider = database.provider
        schema = database.schema = provider.dbschema_cls(provider)
        entities = list(sorted(database.entities.values(), key=attrgetter('_id_')))
        for entity in entities:
            entity._get_pk_columns_()
            table_name = entity._table_

            is_subclass = entity._root_ is not entity
            if is_subclass:
                if table_name is not None: raise NotImplementedError
                table_name = entity._root_._table_
                entity._table_ = table_name
            elif table_name is None:
                table_name = provider.get_default_entity_table_name(entity)
                entity._table_ = table_name
            else: assert isinstance(table_name, (basestring, tuple))

            table = schema.tables.get(table_name)
            if table is None: table = schema.add_table(table_name)
            elif table.entities:
                for e in table.entities:
                    if e._root_ is not entity._root_:
                        raise MappingError("Entities %s and %s cannot be mapped to table %s "
                                           "because they don't belong to the same hierarchy"
                                           % (e, entity, table_name))
            table.entities.add(entity)

            # if entity._base_attrs_: raise NotImplementedError
            for attr in entity._new_attrs_:
                if attr.is_collection:
                    if not isinstance(attr, Set): raise NotImplementedError
                    reverse = attr.reverse
                    if not reverse.is_collection: # many-to-one:
                        if attr.table is not None: raise MappingError(
                            "Parameter 'table' is not allowed for many-to-one attribute %s" % attr)
                        elif attr.columns: raise NotImplementedError(
                            "Parameter 'column' is not allowed for many-to-one attribute %s" % attr)
                        continue
                    # many-to-many:
                    if not isinstance(reverse, Set): raise NotImplementedError
                    if attr.entity.__name__ > reverse.entity.__name__: continue
                    if attr.entity is reverse.entity and attr.name > reverse.name: continue

                    if attr.table:
                        if not reverse.table: reverse.table = attr.table
                        elif reverse.table != attr.table:
                            raise MappingError("Parameter 'table' for %s and %s do not match" % (attr, reverse))
                        table_name = attr.table
                    elif reverse.table: table_name = attr.table = reverse.table
                    else:
                        table_name = provider.get_default_m2m_table_name(attr, reverse)
                        attr.table = reverse.table = table_name

                    m2m_table = schema.tables.get(table_name)
                    if m2m_table is not None:
                        if m2m_table.entities or m2m_table.m2m:
                            if isinstance(table_name, tuple): table_name = '.'.join(table_name)
                            raise MappingError("Table name '%s' is already in use" % table_name)
                        raise NotImplementedError
                    m2m_table = schema.add_table(table_name)
                    m2m_columns_1 = attr.get_m2m_columns(is_reverse=False)
                    m2m_columns_2 = reverse.get_m2m_columns(is_reverse=True)
                    if m2m_columns_1 == m2m_columns_2: raise MappingError(
                        'Different column names should be specified for attributes %s and %s' % (attr, reverse))
                    assert len(m2m_columns_1) == len(reverse.converters)
                    assert len(m2m_columns_2) == len(attr.converters)
                    for column_name, converter in zip(m2m_columns_1 + m2m_columns_2, reverse.converters + attr.converters):
                        m2m_table.add_column(column_name, converter.sql_type(), True)
                    m2m_table.add_index(None, tuple(m2m_table.column_list), is_pk=True)
                    m2m_table.m2m.add(attr)
                    m2m_table.m2m.add(reverse)
                else:
                    columns = attr.get_columns()
                    if not attr.reverse and attr.default is not None:
                        assert len(attr.converters) == 1
                        if not callable(attr.default): attr.default = attr.check(attr.default)
                    assert len(columns) == len(attr.converters)
                    for (column_name, converter) in zip(columns, attr.converters):
                        table.add_column(column_name, converter.sql_type(), attr.is_required and not is_subclass)
            if not table.pk_index:
                if len(entity._pk_columns_) == 1 and entity._pk_.auto: is_pk = "auto"
                else: is_pk = True
                table.add_index(None, get_columns(table, entity._pk_columns_), is_pk)
            for key in entity._keys_:
                column_names = []
                for attr in key: column_names.extend(attr.columns)
                table.add_index(None, get_columns(table, column_names), is_unique=True)
            columns = []
            columns_without_pk = []
            converters = []
            converters_without_pk = []
            for attr in entity._attrs_:
                if attr.is_collection: continue
                columns.extend(attr.columns)  # todo: inheritance
                converters.extend(attr.converters)
                if not attr.is_pk:
                    columns_without_pk.extend(attr.columns)
                    converters_without_pk.extend(attr.converters)
            entity._columns_ = columns
            entity._columns_without_pk_ = columns_without_pk
            entity._converters_ = converters
            entity._converters_without_pk_ = converters_without_pk
        for entity in entities:
            table = schema.tables[entity._table_]
            for attr in entity._new_attrs_:
                if attr.is_collection:
                    reverse = attr.reverse
                    if not reverse.is_collection: continue
                    if not isinstance(attr, Set): raise NotImplementedError
                    if not isinstance(reverse, Set): raise NotImplementedError
                    m2m_table = schema.tables[attr.table]
                    parent_columns = get_columns(table, entity._pk_columns_)
                    child_columns = get_columns(m2m_table, reverse.columns)
                    m2m_table.add_foreign_key(None, child_columns, table, parent_columns)
                    if attr.symmetric:
                        child_columns = get_columns(m2m_table, attr.reverse_columns)
                        m2m_table.add_foreign_key(None, child_columns, table, parent_columns)
                elif attr.reverse and attr.columns:
                    rentity = attr.reverse.entity
                    parent_table = schema.tables[rentity._table_]
                    parent_columns = get_columns(parent_table, rentity._pk_columns_)
                    child_columns = get_columns(table, attr.columns)
                    table.add_foreign_key(None, child_columns, parent_table, parent_columns)

        database.rollback()   
        if create_tables: schema.create_tables()

        if not check_tables and not create_tables: return
        for table in schema.tables.values():
            if isinstance(table.name, tuple): alias = table.name[-1]
            elif isinstance(table.name, basestring): alias = table.name
            else: assert False
            sql_ast = [ SELECT,
                        [ ALL, ] + [ [ COLUMN, alias, column.name ] for column in table.column_list ],
                        [ FROM, [ alias, TABLE, table.name ] ],
                        [ WHERE, [ EQ, [ VALUE, 0 ], [ VALUE, 1 ] ] ]
                      ]
            sql, adapter = database._ast2sql(sql_ast)
            database._exec_sql(sql)

def args2str(args):
    if isinstance(args, (tuple, list)):
        return '[%s]' % ', '.join(map(repr, args))
    elif isinstance(args, dict):
        return '{%s}' % ', '.join('%s:%s' % (repr(key), repr(val)) for key, val in sorted(args.iteritems()))

###############################################################################

class NotLoadedValueType(object):
    def __repr__(self): return 'NOT_LOADED'

NOT_LOADED = NotLoadedValueType()

class DefaultValueType(object):
    def __repr__(self): return 'DEFAULT'

DEFAULT = DefaultValueType()

class NoUndoNeededValueType(object):
    def __repr__(self): return 'NO_UNDO_NEEDED'

NO_UNDO_NEEDED = NoUndoNeededValueType()

class DescWrapper(object):
    def __init__(self, attr):
        self.attr = attr
    def __repr__(self):
        return '<DescWrapper(%s)>' % self.attr
    def __call__(self):
        return self

next_attr_id = count(1).next

class Attribute(object):
    __slots__ = 'is_required', 'is_discriminator', 'is_unique', 'is_indexed', \
                'is_pk', 'is_collection', 'is_ref', 'is_basic', \
                'id', 'pk_offset', 'pk_columns_offset', 'py_type', 'sql_type', 'entity', 'name', \
                'args', 'auto', 'default', 'reverse', 'composite_keys', \
                'column', 'columns', 'col_paths', '_columns_checked', 'converters', 'keyargs'
    @cut_traceback
    def __init__(attr, py_type, *args, **keyargs):
        if attr.__class__ is Attribute: raise TypeError("'Attribute' is abstract type")
        attr.is_required = isinstance(attr, Required)
        attr.is_discriminator = isinstance(attr, Discriminator)
        attr.is_unique = isinstance(attr, Unique)  # Also can be set to True later
        attr.is_indexed = attr.is_unique  # Also can be set to True later
        attr.is_pk = isinstance(attr, PrimaryKey)
        if attr.is_pk: attr.pk_offset = 0
        else: attr.pk_offset = None
        attr.id = next_attr_id()
        if not isinstance(py_type, basestring) and not isinstance(py_type, type):
            if py_type is datetime: raise TypeError(
                'datetime is the module and cannot be used as attribute type. Use datetime.datetime instead')
            raise TypeError('Incorrect type of attribute: %r' % py_type)
        if py_type == 'Entity' or (isinstance(py_type, EntityMeta) and py_type.__name__ == 'Entity'):
            raise TypeError('Cannot link attribute to Entity class. Must use Entity subclass instead')
        attr.py_type = py_type
        attr.is_collection = isinstance(attr, Collection)
        attr.is_ref = not attr.is_collection and isinstance(attr.py_type, (EntityMeta, basestring))
        attr.is_basic = not attr.is_collection and not attr.is_ref
        attr.sql_type = keyargs.pop('sql_type', None)
        attr.entity = attr.name = None
        attr.args = args
        attr.auto = keyargs.pop('auto', False)

        try: attr.default = keyargs.pop('default')
        except KeyError: attr.default = None
        else:
            if attr.default is None and attr.is_required:
                raise TypeError('Default value for required attribute cannot be None' % attr)

        attr.reverse = keyargs.pop('reverse', None)
        if not attr.reverse: pass
        elif not isinstance(attr.reverse, (basestring, Attribute)):
            raise TypeError("Value of 'reverse' option must be name of reverse attribute). Got: %r" % attr.reverse)
        elif not isinstance(attr.py_type, (basestring, EntityMeta)):
            raise TypeError('Reverse option cannot be set for this type: %r' % attr.py_type)

        attr.column = keyargs.pop('column', None)
        attr.columns = keyargs.pop('columns', None)
        if attr.column is not None:
            if attr.columns is not None:
                raise TypeError("Parameters 'column' and 'columns' cannot be specified simultaneously")
            if not isinstance(attr.column, basestring):
                raise TypeError("Parameter 'column' must be a string. Got: %r" % attr.column)
            attr.columns = [ attr.column ]
        elif attr.columns is not None:
            if not isinstance(attr.columns, (tuple, list)):
                raise TypeError("Parameter 'columns' must be a list. Got: %r'" % attr.columns)
            for column in attr.columns:
                if not isinstance(column, basestring):
                    raise TypeError("Items of parameter 'columns' must be strings. Got: %r" % attr.columns)
            if len(attr.columns) == 1: attr.column = attr.columns[0]
        else: attr.columns = []
        attr.col_paths = []
        attr._columns_checked = False
        attr.composite_keys = []
        attr.keyargs = keyargs
        attr.converters = []
    def _init_(attr, entity, name):
        attr.entity = entity
        attr.name = name
    @cut_traceback
    def __repr__(attr):
        owner_name = not attr.entity and '?' or attr.entity.__name__
        return '%s.%s' % (owner_name, attr.name or '?')
    def check(attr, val, obj=None, entity=None, from_db=False):
        assert val is not NOT_LOADED
        if entity is not None: pass
        elif obj is not None: entity = obj.__class__
        else: entity = attr.entity

        if val is DEFAULT:
            default = attr.default
            if default is None:
                if attr.is_required and not attr.auto: raise ConstraintError(
                    'Required attribute %s.%s does not specified' % (entity.__name__, attr.name))
                return None
            if callable(default): val = default()
            else: val = default
        elif val is None: return val
        
        reverse = attr.reverse
        if not reverse:
            if isinstance(val, Entity): raise TypeError('Attribute %s.%s must be of %s type. Got: %s'
                % (attr.entity.__name__, attr.name, attr.py_type.__name__, val))
            if attr.converters:
                if len(attr.converters) != 1: raise NotImplementedError
                converter = attr.converters[0]
                if converter is not None:
                    if from_db: return converter.sql2py(val)
                    else: return converter.validate(val)
            return attr.py_type(val)

        if not isinstance(val, reverse.entity):
            raise ConstraintError('Value of attribute %s.%s must be an instance of %s. Got: %s'
                                  % (entity.__name__, attr.name, reverse.entity.__name__, val))
        if obj is not None: cache = obj._cache_
        else: cache = entity._get_cache_()
        if cache is not val._cache_:
            raise TransactionError('An attempt to mix objects belongs to different caches')
        return val
    def load(attr, obj):
        if not attr.columns:
            reverse = attr.reverse
            assert reverse is not None and reverse.columns
            objects = reverse.entity._find_in_db_({reverse : obj}, 1)
            if not objects:
                obj._vals_[attr.name] = None
                return None
            elif len(objects) == 1:
                dbval = objects[0]
                assert obj._vals_[attr.name] == dbval
                return dbval
            else: assert False
        obj._load_()
        return obj._vals_[attr.name]
    @cut_traceback
    def __get__(attr, obj, cls=None):
        if obj is None: return attr
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        result = attr.get(obj)
        if attr.pk_offset is not None: return result
        bit = obj._bits_[attr]
        wbits = obj._wbits_
        if wbits is not None and not wbits & bit: obj._rbits_ |= bit
        return result
    def get(attr, obj):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        val = obj._vals_.get(attr.name, NOT_LOADED)
        if val is NOT_LOADED: val = attr.load(obj)
        return val
    @cut_traceback
    def __set__(attr, obj, new_val, undo_funcs=None):
        cache = obj._cache_
        if not cache.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        is_reverse_call = undo_funcs is not None
        reverse = attr.reverse
        new_val = attr.check(new_val, obj, from_db=False)
        pkval = obj._pkval_
        if attr.pk_offset is not None:
            if pkval is None: pass
            elif obj._pk_is_composite_:
                if new_val == pkval[attr.pk_offset]: return
            elif new_val == pkval: return
            raise TypeError('Cannot change value of primary key')
        old_val =  obj._vals_.get(attr.name, NOT_LOADED)
        if old_val is NOT_LOADED and reverse and not reverse.is_collection:
            old_val = attr.load(obj)
        status = obj._status_
        wbits = obj._wbits_
        if wbits is not None:
            obj._wbits_ = wbits | obj._bits_[attr]
            if status != 'updated':
                if status in ('loaded', 'saved'): cache.to_be_checked.append(obj)
                else: assert status == 'locked'
                obj._status_ = 'updated'
                cache.updated.add(obj)
        if not attr.reverse and not attr.is_indexed:
            obj._vals_[attr.name] = new_val
            return
        if not is_reverse_call: undo_funcs = []
        undo = []
        def undo_func():
            obj._status_ = status
            obj._wbits_ = wbits
            if wbits == 0: cache.updated.remove(obj)
            if status in ('loaded', 'saved'):
                to_be_checked = cache.to_be_checked
                if to_be_checked and to_be_checked[-1] is obj: to_be_checked.pop()
                assert obj not in to_be_checked
            obj._vals_[attr.name] = old_val
            for index, old_key, new_key in undo:
                if new_key is NO_UNDO_NEEDED: pass
                else: del index[new_key]
                if old_key is NO_UNDO_NEEDED: pass
                else: index[old_key] = obj
        undo_funcs.append(undo_func)
        if old_val == new_val: return
        try:
            if attr.is_unique:
                cache.update_simple_index(obj, attr, old_val, new_val, undo)
            for attrs, i in attr.composite_keys:
                get = obj._vals_.get
                vals = [ get(a.name, NOT_LOADED) for a in attrs ]
                currents = tuple(vals)
                vals[i] = new_val
                vals = tuple(vals)
                cache.update_composite_index(obj, attrs, currents, vals, undo)

            obj._vals_[attr.name] = new_val
                
            if not reverse: pass
            elif not is_reverse_call: attr.update_reverse(obj, old_val, new_val, undo_funcs)
            elif old_val not in (None, NOT_LOADED):
                if not reverse.is_collection:
                    if new_val is not None: reverse.__set__(old_val, None, undo_funcs)
                elif isinstance(reverse, Set):
                    reverse.reverse_remove((old_val,), obj, undo_funcs)
                else: raise NotImplementedError
        except:
            if not is_reverse_call:
                for undo_func in reversed(undo_funcs): undo_func()
            raise
    def db_set(attr, obj, new_dbval, is_reverse_call=False):
        cache = obj._cache_
        assert cache.is_alive
        assert obj._status_ not in ('created', 'deleted', 'cancelled')
        assert attr.pk_offset is None
        if new_dbval is NOT_LOADED: assert is_reverse_call
        else: new_dbval = attr.check(new_dbval, obj, from_db=True)
        old_dbval = obj._dbvals_.get(attr.name, NOT_LOADED)

        if attr.py_type is float:
            if old_dbval is NOT_LOADED: pass
            elif attr.converters[0].equals(old_dbval, new_dbval): return
        elif old_dbval == new_dbval: return

        bit = obj._bits_[attr]
        if obj._rbits_ & bit:
            assert old_dbval is not NOT_LOADED
            if new_dbval is NOT_LOADED: diff = ''
            else: diff = ' (was: %s, now: %s)' % (old_dbval, new_dbval)
            raise UnrepeatableReadError(
                'Value of %s.%s for %s was updated outside of current transaction%s'
                % (obj.__class__.__name__, attr.name, obj, diff))

        if new_dbval is NOT_LOADED: obj._dbvals_.pop(attr.name, None)
        else: obj._dbvals_[attr.name] = new_dbval
            
        wbit = bool(obj._wbits_ & bit)
        if not wbit:
            old_val = obj._vals_.get(attr.name, NOT_LOADED)
            assert old_val == old_dbval
            if attr.is_indexed:
                cache = obj._cache_
                if attr.is_unique: cache.db_update_simple_index(obj, attr, old_val, new_dbval)
                for attrs, i in attr.composite_keys:
                    vals = [ obj._vals_.get(a.name, NOT_LOADED) for a in attrs ]
                    old_vals = tuple(vals)
                    vals[i] = new_dbval
                    new_vals = tuple(vals)
                    cache.db_update_composite_index(obj, attrs, old_vals, new_vals)
            if new_dbval is NOT_LOADED: obj._vals_.pop(attr.name, None)
            else: obj._vals_[attr.name] = new_dbval

        reverse = attr.reverse
        if not reverse: pass
        elif not is_reverse_call: attr.db_update_reverse(obj, old_dbval, new_dbval)
        elif old_dbval not in (None, NOT_LOADED):
            if not reverse.is_collection:
                if new_dbval is not NOT_LOADED: reverse.db_set(old_dbval, NOT_LOADED, is_reverse_call=True)
            elif isinstance(reverse, Set):
                reverse.db_reverse_remove((old_dbval,), obj)
            else: raise NotImplementedError
    def update_reverse(attr, obj, old_val, new_val, undo_funcs):
        reverse = attr.reverse
        if not reverse.is_collection:
            if old_val not in (None, NOT_LOADED): reverse.__set__(old_val, None, undo_funcs)
            if new_val is not None: reverse.__set__(new_val, obj, undo_funcs)
        elif isinstance(reverse, Set):
            if old_val not in (None, NOT_LOADED): reverse.reverse_remove((old_val,), obj, undo_funcs)
            if new_val is not None: reverse.reverse_add((new_val,), obj, undo_funcs)
        else: raise NotImplementedError
    def db_update_reverse(attr, obj, old_dbval, new_dbval):
        reverse = attr.reverse
        if not reverse.is_collection:
            if old_dbval not in (None, NOT_LOADED): reverse.db_set(old_dbval, NOT_LOADED, True)
            if new_dbval is not None: reverse.db_set(new_dbval, obj, True)
        elif isinstance(reverse, Set):
            if old_dbval not in (None, NOT_LOADED): reverse.db_reverse_remove((old_dbval,), obj)
            if new_dbval is not None: reverse.db_reverse_add((new_dbval,), obj)
        else: raise NotImplementedError
    def __delete__(attr, obj):
        raise NotImplementedError
    def get_raw_values(attr, val):
        reverse = attr.reverse
        if not reverse: return (val,)
        rentity = reverse.entity
        if val is None: return rentity._pk_nones_
        return val._get_raw_pkval_()
    def get_columns(attr):
        assert not attr.is_collection
        assert not isinstance(attr.py_type, basestring)
        if attr._columns_checked: return attr.columns

        provider = attr.entity._database_.provider
        reverse = attr.reverse
        if not reverse: # attr is not part of relationship
            if not attr.columns: attr.columns = [ attr.name ]
            elif len(attr.columns) > 1: raise MappingError("Too many columns were specified for %s" % attr)
            attr.col_paths = [ attr.name ]
            attr.converters = [ provider.get_converter_by_attr(attr) ]
        else:
            def generate_columns():
                reverse_pk_columns = reverse.entity._get_pk_columns_()
                reverse_pk_col_paths = reverse.entity._pk_paths_
                if not attr.columns:
                    attr.columns = provider.get_default_column_names(attr, reverse_pk_columns)
                elif len(attr.columns) != len(reverse_pk_columns): raise MappingError(
                    'Invalid number of columns specified for %s' % attr)
                attr.col_paths = [ '-'.join((attr.name, paths)) for paths in reverse_pk_col_paths ]
                attr.converters = []
                for a in reverse.entity._pk_attrs_:
                    attr.converters.extend(a.converters)

            if reverse.is_collection: # one-to-many:
                generate_columns()
            # one-to-one:
            elif attr.is_required:
                assert not reverse.is_required
                generate_columns()
            elif attr.columns: generate_columns()
            elif reverse.columns: pass
            elif attr.entity.__name__ > reverse.entity.__name__: pass
            else: generate_columns()
        attr._columns_checked = True
        if len(attr.columns) == 1: attr.column = attr.columns[0]
        else: attr.column = None
        return attr.columns
    @property
    def asc(attr):
        return attr
    @property
    def desc(attr):
        return DescWrapper(attr)
    def describe(attr):
        t = attr.py_type
        if isinstance(t, type): t = t.__name__            
        result = "%s(%s)" % (attr.__class__.__name__, t)
        return "%s = %s" % (attr.name,result)

class Optional(Attribute):
    __slots__ = []
    
class Required(Attribute):
    __slots__ = []
    def check(attr, val, obj=None, entity=None, from_db=False):
        if val is not None:
            val = Attribute.check(attr, val, obj, entity, from_db)  # val may be changed to None here
        if val is None and not attr.auto:
            if entity is not None: pass
            elif obj is not None: entity = obj.__class__
            else: entity = attr.entity
            if obj is None: raise ConstraintError(
                'Required attribute %s.%s cannot be set to None' % (entity.__name__, attr.name))
            else: raise ConstraintError(
                'Required attribute %s.%s for %r cannot be set to None' % (entity.__name__, attr.name, obj))
        return val

class Discriminator(Required):
    __slots__ = [ 'code2cls' ]
    def __init__(attr, py_type, *args, **keyargs):
        Attribute.__init__(attr, py_type, *args, **keyargs)
        attr.code2cls = {}
    def _init_(attr, entity, name):
        if entity._root_ is not entity: raise ERDiagramError(
            'Discriminator attribute %s cannot be declared in subclass' % attr)
        Required._init_(attr, entity, name)
        entity._discriminator_attr_ = attr
    @staticmethod
    def create_default_attr(entity):
        if hasattr(entity, 'classtype'): raise ERDiagramError(
            "Cannot create discriminator column for %s automatically "
            "because name 'classtype' is already in use" % entity.__name__)
        attr = Discriminator(str, column='classtype')
        attr._init_(entity, 'classtype')
        entity._attrs_.append(attr)
        entity._new_attrs_.append(attr)
        entity._adict_['classtype'] = attr
        attr.process_entity_inheritance(entity)
    def process_entity_inheritance(attr, entity):
        if '_discriminator_' not in entity.__dict__:
            entity._discriminator_ = entity.__name__
        discr_value = entity._discriminator_
        if discr_value is None:
            discr_value = entity._discriminator_ = entity.__name__
        discr_type = type(discr_value)
        for code, cls in attr.code2cls.items():
            if type(code) != discr_type: raise ERDiagramError(
                'Discriminator values %r and %r of entities %s and %s have different types'
                % (code, discr_value, cls, entity))
        attr.code2cls[discr_value] = entity
    def check(attr, val, obj=None, entity=None, from_db=False):
        if from_db: return val
        elif val is DEFAULT:
            assert entity is not None
            return entity._discriminator_
        return Attribute.check(attr, val, obj, entity)
    def load(attr, obj):
        raise AssertionError
    def __get__(attr, obj, cls=None):
        if obj is None: return attr
        return obj._discriminator_
    def __set__(attr, obj, new_val):
        raise TypeError('Cannot assign value to discriminator attribute')
    def db_set(attr, obj, new_dbval):
        assert False
    def update_reverse(attr, obj, old_val, new_val, undo_funcs):
        assert False

class Unique(Required):
    __slots__ = []
    def __new__(cls, *args, **keyargs):
        is_pk = issubclass(cls, PrimaryKey)
        if not args: raise TypeError('Invalid count of positional arguments')
        attrs = tuple(a for a in args if isinstance(a, Attribute))
        non_attrs = [ a for a in args if not isinstance(a, Attribute) ]
        if attrs and (non_attrs or keyargs): raise TypeError('Invalid arguments')
        cls_dict = sys._getframe(1).f_locals
        keys = cls_dict.setdefault('_keys_', {})

        if not attrs:
            result = Required.__new__(cls, *args, **keyargs)
            keys[(result,)] = is_pk
            return result

        for attr in attrs:
            if attr.is_collection or attr.is_discriminator or (
                    is_pk and not attr.is_required and not attr.auto):
                raise TypeError('%s attribute cannot be part of %s'
                                % (attr.__class__.__name__, is_pk and 'primary key' or 'unique index'))
            attr.is_indexed = True
        if len(attrs) == 1:
            attr = attrs[0]
            if attr.is_required: raise TypeError('Invalid declaration')
            attr.is_unique = True
        else:
            for i, attr in enumerate(attrs): attr.composite_keys.append((attrs, i))
        keys[attrs] = is_pk
        return None

def populate_criteria_list(criteria_list, columns, converters, params_count=0, table_alias=None):
    assert len(columns) == len(converters)
    for column, converter in zip(columns, converters):
        if converter is not None:
            criteria_list.append([EQ, [ COLUMN, table_alias, column ], [ PARAM, params_count, converter ] ])
        else:
            criteria_list.append([IS_NULL, [COLUMN, None, column]])
        params_count += 1
    return params_count

class PrimaryKey(Unique):
    __slots__ = []

class Collection(Attribute):
    __slots__ = 'table', 'cached_load_sql', 'cached_add_m2m_sql', 'cached_remove_m2m_sql', 'wrapper_class', \
                'symmetric', 'reverse_column', 'reverse_columns'
    def __init__(attr, py_type, *args, **keyargs):
        if attr.__class__ is Collection: raise TypeError("'Collection' is abstract type")
        table = keyargs.pop('table', None)  # TODO: rename table to link_table or m2m_table
        if table is not None and not isinstance(table, basestring):
            if not isinstance(table, (list, tuple)): raise TypeError(
                "Parameter 'table' must be a string. Got: %r" % table)
            for name_part in table:
                if not isinstance(name_part, basestring): raise TypeError(
                    'Each part of table name must be a string. Got: %r' % name_part)
            table = tuple(table)
        attr.table = table
        Attribute.__init__(attr, py_type, *args, **keyargs)
        if attr.default is not None: raise TypeError('Default value could not be set for collection attribute')
        if attr.auto: raise TypeError("'auto' option could not be set for collection attribute")

        attr.reverse_column = attr.keyargs.pop('reverse_column', None)
        attr.reverse_columns = attr.keyargs.pop('reverse_columns', None)
        if attr.reverse_column is not None:
            if attr.reverse_columns is not None and attr.reverse_columns != [ attr.reverse_column ]:
                raise TypeError("Parameters 'reverse_column' and 'reverse_columns' cannot be specified simultaneously")
            if not isinstance(attr.reverse_column, basestring):
                raise TypeError("Parameter 'reverse_column' must be a string. Got: %r" % attr.reverse_column)
            attr.reverse_columns = [ attr.reverse_column ]
        elif attr.reverse_columns is not None:
            if not isinstance(attr.reverse_columns, (tuple, list)):
                raise TypeError("Parameter 'reverse_columns' must be a list. Got: %r" % attr.reverse_columns)
            for reverse_column in attr.reverse_columns:
                if not isinstance(reverse_column, basestring):
                    raise TypeError("Parameter 'reverse_columns' must be a list of strings. Got: %r" % attr.reverse_columns)
            if len(attr.reverse_columns) == 1: attr.reverse_column = attr.reverse_columns[0]
        else: attr.reverse_columns = []

        for option in attr.keyargs: raise TypeError('Unknown option %r' % option)
        attr.cached_load_sql = {}
        attr.cached_add_m2m_sql = None
        attr.cached_remove_m2m_sql = None
    def _init_(attr, entity, name):
        Attribute._init_(attr, entity, name)
        attr.symmetric = (attr.py_type == entity.__name__ and attr.reverse == name)
        if not attr.symmetric and attr.reverse_columns: raise TypeError(
            "'reverse_column' and 'reverse_columns' options can be set for symmetric relations only")
        if attr.reverse_columns and len(attr.reverse_columns) != len(attr.columns):
            raise TypeError("Number of columns and reverse columns for symmetric attribute %s do not match" % attr)
    def load(attr, obj):
        assert False, 'Abstract method'
    def __get__(attr, obj, cls=None):
        assert False, 'Abstract method'
    def __set__(attr, obj, val):
        assert False, 'Abstract method'
    def __delete__(attr, obj):
        assert False, 'Abstract method'
    def prepare(attr, obj, val, fromdb=False):
        assert False, 'Abstract method'
    def set(attr, obj, val, fromdb=False):
        assert False, 'Abstract method'

EMPTY = ()

class SetData(set):
    __slots__ = 'is_fully_loaded', 'added', 'removed'
    def __init__(setdata):
        setdata.is_fully_loaded = False
        setdata.added = setdata.removed = EMPTY

class Set(Collection):
    __slots__ = []
    def check(attr, val, obj=None, entity=None, from_db=False):
        assert val is not NOT_LOADED
        if val is None or val is DEFAULT: return set()
        if entity is not None: pass
        elif obj is not None: entity = obj.__class__
        else: entity = attr.entity
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        if isinstance(val, reverse.entity): items = set((val,))
        else:
            rentity = reverse.entity
            try: items = set(val)
            except TypeError: raise TypeError('Item of collection %s.%s must be an instance of %s. Got: %r'
                                              % (entity.__name__, attr.name, rentity.__name__, val))
            for item in items:
                if not isinstance(item, rentity):
                    raise TypeError('Item of collection %s.%s must be an instance of %s. Got: %r'
                                    % (entity.__name__, attr.name, rentity.__name__, item))
        if obj is not None: cache = obj._cache_
        else: cache = entity._get_cache_()
        for item in items:
            if item._cache_ is not cache:
                raise TransactionError('An attempt to mix objects belongs to different caches')
        return items
    def load(attr, obj):
        assert obj._status_ not in ('deleted', 'cancelled')
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED: setdata = obj._vals_[attr.name] = SetData()
        elif setdata.is_fully_loaded: return setdata
        entity = attr.entity
        reverse = attr.reverse
        rentity = reverse.entity
        if reverse is None: raise NotImplementedError
        database = obj._database_
        objects = [ obj ]
        setdata_list = [ setdata ]
        cache = obj._cache_
        assert cache.is_alive
        counter = cache.collection_statistics.setdefault(attr, 0)
        if counter:
            pk_index = cache.indexes.get(entity._pk_)
            max_batch_size = database.provider.max_params_count // len(entity._pk_columns_)
            for obj2 in pk_index.itervalues():
                if obj2 is obj: continue
                if obj2._status_ in ('created', 'deleted', 'cancelled'): continue
                setdata2 = obj2._vals_.get(attr.name, NOT_LOADED)
                if setdata2 is NOT_LOADED: setdata2 = obj2._vals_[attr.name] = SetData()
                elif setdata2.is_fully_loaded: continue
                objects.append(obj2)
                setdata_list.append(setdata2)
                if len(objects) >= max_batch_size: break

        value_dict = {}
        for i, obj2 in enumerate(objects):
            for j, val in enumerate(obj2._get_raw_pkval_()): value_dict[i, j] = val
            
        if not reverse.is_collection:
            sql, adapter, attr_offsets = rentity._construct_batchload_sql_(len(objects), reverse)
            arguments = adapter(value_dict)
            cursor = database._exec_sql(sql, arguments)
            items = rentity._fetch_objects(cursor, attr_offsets)
        else:
            rentity = reverse.entity
            sql, adapter = attr.construct_sql_m2m(len(objects))
            arguments = adapter(value_dict)
            cursor = database._exec_sql(sql, arguments)
            pk_len = len(entity._pk_columns_)
            d = {}
            if len(objects) > 1:
                for row in cursor.fetchall():
                    obj2 = entity._get_by_raw_pkval_(row[:pk_len])
                    item = rentity._get_by_raw_pkval_(row[pk_len:])
                    items = d.get(obj2)
                    if items is None: items = d[obj2] = set()
                    items.add(item)
            else: d[obj] = set(imap(rentity._get_by_raw_pkval_, cursor.fetchall()))
            for obj2, items in d.iteritems():
                setdata2 = obj2._vals_.get(attr.name, NOT_LOADED)
                if setdata2 is NOT_LOADED: setdata2 = obj._vals_[attr.name] = SetData()
                else:
                    phantoms = setdata2 - items
                    phantoms.difference_update(setdata2.added)
                    if phantoms: raise UnrepeatableReadError(
                        'Phantom object %r disappeared from collection %r.%s' % (phantoms.pop(), obj, attr.name))
                items -= setdata2
                items.difference_update(setdata2.removed)
                setdata2 |= items
                reverse.db_reverse_add(items, obj2)

        for setdata2 in setdata_list: setdata2.is_fully_loaded = True
        cache.collection_statistics[attr] = counter + 1
        return setdata
    def construct_sql_m2m(attr, batch_size=1):
        cached_sql = attr.cached_load_sql.get(batch_size)
        if cached_sql is not None: return cached_sql
        reverse = attr.reverse
        assert reverse is not None and reverse.is_collection and issubclass(reverse.py_type, Entity)
        table_name = attr.table
        assert table_name is not None
        select_list = [ ALL ]
        if not attr.symmetric:
            columns = attr.columns
            rcolumns = reverse.columns
            rconverters = reverse.converters
        else:
            columns = attr.reverse_columns
            rcolumns = attr.columns
            rconverters = attr.converters
        if batch_size > 1:
            select_list.extend([ COLUMN, 'T1', column ] for column in rcolumns)
        select_list.extend([ COLUMN, 'T1', column ] for column in columns)
        from_list = [ FROM, [ 'T1', TABLE, table_name ]]
        database = attr.entity._database_
        
        if batch_size == 1:
            criteria_list = []
            for i, (column, converter) in enumerate(zip(rcolumns, rconverters)):
                criteria_list.append([EQ, [COLUMN, 'T1', column], [ PARAM, (0, i), converter ]])
        elif len(rcolumns) == 1:
            converter = rconverters[0]
            criteria_list = [ [ IN, [ COLUMN, None, rcolumns[0] ],
                                   [ [ PARAM, (i, 0), converter ] for i in xrange(batch_size) ] ] ]
        elif database.provider.translator_cls.row_value_syntax:
            criteria_list = [ [ IN, [ ROW ] + [ [ COLUMN, None, column ] for column in rcolumns ],
                                   [ [ ROW ] + [ [ PARAM, (i, j), converter ] for j, converter in enumerate(rconverters) ]
                                     for i in xrange(batch_size) ] ] ]
        else:
            pairs = zip(rcolumns, rconverters)
            criteria_list = [ [ OR ] + [ [ AND ] + [ [ EQ, [ COLUMN, None, column ], [ PARAM, (i, j), converter ] ]
                                                   for j, (column, converter) in enumerate(pairs) ]
                                       for i in xrange(batch_size) ] ]

        sql_ast = [ SELECT, select_list, from_list, [ WHERE ] + criteria_list ]
        sql, adapter = attr.cached_load_sql[batch_size] = database._ast2sql(sql_ast)
        return sql, adapter
    def copy(attr, obj):
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED or not setdata.is_fully_loaded: setdata = attr.load(obj)
        reverse = attr.reverse
        if reverse.is_collection or reverse.pk_offset is not None: return setdata.copy()
        for item in setdata:
            bit = item._bits_[reverse]
            wbits = item._wbits_
            if wbits is not None and not wbits & bit: item._rbits_ |= bit
        return setdata.copy()
    @cut_traceback
    def __get__(attr, obj, cls=None):
        if obj is None: return attr
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        rentity = attr.py_type
        wrapper_class = rentity._get_set_wrapper_subclass_()
        return wrapper_class(obj, attr)
    @cut_traceback
    def __set__(attr, obj, new_items, undo_funcs=None):
        cache = obj._cache_
        if not cache.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        new_items = attr.check(new_items, obj)
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED:
            if obj._status_ == 'created':
                setdata = obj._vals_[attr.name] = SetData()
                setdata.is_fully_loaded = True
                if not new_items: return
            else: setdata = attr.load(obj)
        elif not setdata.is_fully_loaded: setdata = attr.load(obj)
        to_add = set(ifilterfalse(setdata.__contains__, new_items))
        to_remove = setdata - new_items
        if undo_funcs is None: undo_funcs = []
        try:
            if not reverse.is_collection:
                for item in to_remove: reverse.__set__(item, None, undo_funcs)
                for item in to_add: reverse.__set__(item, obj, undo_funcs)
            else:
                reverse.reverse_remove(to_remove, obj, undo_funcs)
                reverse.reverse_add(to_add, obj, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        setdata.clear()
        setdata.update(new_items)
        if to_add:
            if setdata.added is EMPTY: setdata.added = to_add
            else: setdata.added.update(to_add)
            if setdata.removed is not EMPTY: setdata.removed -= to_add
        if to_remove:
            if setdata.removed is EMPTY: setdata.removed = to_remove
            else: setdata.removed.update(to_remove)
            if setdata.added is not EMPTY: setdata.added -= to_remove
        cache.modified_collections.setdefault(attr, set()).add(obj)
    def __delete__(attr, obj):
        raise NotImplementedError
    def reverse_add(attr, objects, item, undo_funcs):
        undo = []
        cache = item._cache_
        objects_with_modified_collections = cache.modified_collections.setdefault(attr, set())
        for obj in objects:
            setdata = obj._vals_.get(attr.name, NOT_LOADED)
            if setdata is NOT_LOADED:
                setdata = obj._vals_[attr.name] = SetData()
            if setdata.added is EMPTY: setdata.added = set()  
            elif item in setdata.added: raise AssertionError
            in_setdata = item in setdata
            in_removed = item in setdata.removed
            was_modified_earlier = obj in objects_with_modified_collections
            undo.append((obj, in_setdata, in_removed, was_modified_earlier))
            if not in_setdata: setdata.add(item)
            setdata.added.add(item)
            if in_removed: setdata.removed.remove(item)
            objects_with_modified_collections.add(obj)
        def undo_func():
            for obj, in_setdata, in_removed, was_modified_earlier in undo:
                setdata = obj._vals_[attr.name]
                setdata.added.remove(item)
                if not in_setdata: setdata.remove(item)
                if in_removed: setdata.removed.add(item)
                if not was_modified_earlier: objects_with_modified_collections.remove(obj)
        undo_funcs.append(undo_func)
    def db_reverse_add(attr, objects, item):
        for obj in objects:
            setdata = obj._vals_.get(attr.name, NOT_LOADED)
            if setdata is NOT_LOADED:
                setdata = obj._vals_[attr.name] = SetData()
            elif setdata.is_fully_loaded:
                raise UnrepeatableReadError('Phantom object %r appeared in collection %r.%s' % (item, obj, attr.name))
            setdata.add(item)
    def reverse_remove(attr, objects, item, undo_funcs):
        undo = []
        cache = item._cache_
        objects_with_modified_collections = cache.modified_collections.setdefault(attr, set())
        for obj in objects:
            setdata = obj._vals_.get(attr.name, NOT_LOADED)
            if setdata is NOT_LOADED:
                setdata = obj._vals_[attr.name] = SetData()
            if setdata.removed is EMPTY: setdata.removed = set()
            elif item in setdata.removed: raise AssertionError
            in_setdata = item in setdata
            in_added = item in setdata.added
            was_modified_earlier = obj in objects_with_modified_collections
            undo.append((obj, in_setdata, in_added, was_modified_earlier))
            if in_setdata: setdata.remove(item)
            if in_added: setdata.added.remove(item)
            setdata.removed.add(item)
            objects_with_modified_collections.add(obj)
        def undo_func():
            for obj, in_setdata, in_removed, was_modified_earlier in undo:
                setdata = obj._vals_[attr.name]
                if in_added: setdata.added.add(item)
                if in_setdata: setdata.add(item)
                setdata.removed.remove(item)
                if not was_modified_earlier: objects_with_modified_collections.remove(obj)
        undo_funcs.append(undo_func)
    def db_reverse_remove(attr, objects, item):
        raise AssertionError
    def get_m2m_columns(attr, is_reverse=False):
        entity = attr.entity
        if attr.symmetric:
            if attr._columns_checked:
                if not is_reverse: return attr.columns
                else: return attr.reverse_columns
            if attr.columns:
                if len(attr.columns) != len(entity._get_pk_columns_()): raise MappingError(
                    'Invalid number of columns for %s' % reverse)
            else:
                provider = attr.entity._database_.provider
                attr.columns = provider.get_default_m2m_column_names(entity)
            attr.converters = entity._pk_converters_
            if not attr.reverse_columns:
                attr.reverse_columns = [ column + '_2' for column in attr.columns ]
            attr._columns_checked = True
            if not is_reverse: return attr.columns
            else: return attr.reverse_columns

        reverse = attr.reverse
        if attr._columns_checked: return attr.reverse.columns
        elif reverse.columns:
            if len(reverse.columns) != len(entity._get_pk_columns_()): raise MappingError(
                'Invalid number of columns for %s' % reverse)
        else:
            provider = attr.entity._database_.provider
            reverse.columns = provider.get_default_m2m_column_names(entity)
        reverse.converters = entity._pk_converters_
        attr._columns_checked = True
        return reverse.columns
    def remove_m2m(attr, removed):
        entity = attr.entity
        database = entity._database_
        cached_sql = attr.cached_remove_m2m_sql
        if cached_sql is None:
            reverse = attr.reverse
            table_name = attr.table
            assert table_name is not None
            where_list = [ WHERE ]
            if attr.symmetric:
                columns = attr.columns + attr.reverse_columns
                converters = attr.converters + attr.converters
            else:
                columns = reverse.columns + attr.columns
                converters = reverse.converters + attr.converters
            for i, (column, converter) in enumerate(zip(columns, converters)):
                where_list.append([ EQ, [COLUMN, None, column], [ PARAM, i, converter ] ])
            sql_ast = [ DELETE, table_name, where_list ]
            sql, adapter = database._ast2sql(sql_ast)
            attr.cached_remove_m2m_sql = sql, adapter
        else: sql, adapter = cached_sql
        arguments_list = [ adapter(obj._get_raw_pkval_() + robj._get_raw_pkval_())
                           for obj, robj in removed ]
        database._exec_sql_many(sql, arguments_list)
    def add_m2m(attr, added):
        entity = attr.entity
        database = entity._database_
        cached_sql = attr.cached_add_m2m_sql
        if cached_sql is None:
            reverse = attr.reverse
            table_name = attr.table
            assert table_name is not None
            if attr.symmetric:
                columns = attr.columns + attr.reverse_columns
                converters = attr.converters + attr.converters
            else:
                columns = reverse.columns + attr.columns
                converters = reverse.converters + attr.converters
            params = [ [PARAM, i, converter] for i, converter in enumerate(converters) ]
            sql_ast = [ INSERT, table_name, columns, params ]
            sql, adapter = database._ast2sql(sql_ast)
            attr.cached_add_m2m_sql = sql, adapter
        else: sql, adapter = cached_sql
        arguments_list = [ adapter(obj._get_raw_pkval_() + robj._get_raw_pkval_())
                           for obj, robj in added ]
        database._exec_sql_many(sql, arguments_list)

class SetWrapper(object):
    __slots__ = '_obj_', '_attr_'
    _parent_ = None
    def __init__(wrapper, obj, attr):
        wrapper._obj_ = obj
        wrapper._attr_ = attr
    @cut_traceback
    def copy(wrapper):
        if not wrapper._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        return wrapper._attr_.copy(wrapper._obj_)
    @cut_traceback
    def __repr__(wrapper):
        if wrapper._obj_._cache_.is_alive:
            size = len(wrapper)
            if size == 1: size_str = ' (1 item)'
            else: size_str = ' (%d items)' % size
        else: size_str = ''
        return '<%r.%s%s>' % (wrapper._obj_, wrapper._attr_.name, size_str)
    @cut_traceback
    def __str__(wrapper):
        return str(wrapper.copy())
    @cut_traceback
    def __nonzero__(wrapper):
        attr = wrapper._attr_
        obj = wrapper._obj_
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED: setdata = attr.load(obj)
        if setdata: return True
        if not setdata.is_fully_loaded: setdata = attr.load(obj)
        return bool(setdata)
    @cut_traceback
    def __len__(wrapper):
        attr = wrapper._attr_
        obj = wrapper._obj_
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED or not setdata.is_fully_loaded: setdata = attr.load(obj)
        return len(setdata)
    @cut_traceback
    def __iter__(wrapper):
        return iter(wrapper.copy())
    @cut_traceback
    def __eq__(wrapper, other):
        if isinstance(other, SetWrapper):
            if wrapper._obj_ is other._obj_ and wrapper._attr_ is other._attr_: return True
            else: other = other.copy()
        elif not isinstance(other, set): other = set(other)
        items = wrapper.copy()
        return items == other
    @cut_traceback
    def __ne__(wrapper, other):
        return not wrapper.__eq__(other)
    @cut_traceback
    def __add__(wrapper, new_items):
        return wrapper.copy().union(new_items)
    @cut_traceback
    def __sub__(wrapper, items):
        return wrapper.copy().difference(items)
    @cut_traceback
    def __contains__(wrapper, item):
        obj = wrapper._obj_
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        attr = wrapper._attr_
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is not NOT_LOADED:
            if item in setdata: return True
            if setdata.is_fully_loaded: return False
        setdata = attr.load(obj)
        return item in setdata
    @cut_traceback
    def add(wrapper, new_items):
        obj = wrapper._obj_
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        attr = wrapper._attr_
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        new_items = attr.check(new_items, obj)
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED: setdata = obj._vals_[attr.name] = SetData()
        new_items.difference_update(setdata.added)
        undo_funcs = []
        try:
            if not reverse.is_collection:
                  for item in new_items - setdata: reverse.__set__(item, obj, undo_funcs)
            else: reverse.reverse_add(new_items - setdata, obj, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        setdata.update(new_items)
        if setdata.added is EMPTY: setdata.added = new_items
        else: setdata.added.update(new_items)
        if setdata.removed is not EMPTY: setdata.removed -= new_items
        obj._cache_.modified_collections.setdefault(attr, set()).add(obj)
    @cut_traceback
    def __iadd__(wrapper, items):
        wrapper.add(items)
        return wrapper
    @cut_traceback
    def remove(wrapper, items):
        obj = wrapper._obj_
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        attr = wrapper._attr_
        reverse = attr.reverse
        if not reverse: raise NotImplementedError
        items = attr.check(items, obj)
        setdata = obj._vals_.get(attr.name, NOT_LOADED)
        if setdata is NOT_LOADED or not setdata.is_fully_loaded:
            setdata = attr.load(obj) # TODO: Load only the necessary objects
        items.difference_update(setdata.removed)
        undo_funcs = []
        try:
            if not reverse.is_collection:
                for item in (items & setdata): reverse.__set__(item, None, undo_funcs)
            else: reverse.reverse_remove(items & setdata, obj, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        setdata -= items
        if setdata.added is not EMPTY: setdata.added -= items
        if setdata.removed is EMPTY: setdata.removed = items
        else: setdata.removed.update(items)
        obj._cache_.modified_collections.setdefault(attr, set()).add(obj)
    @cut_traceback
    def __isub__(wrapper, items):
        wrapper.remove(items)
        return wrapper
    @cut_traceback
    def clear(wrapper):
        obj = wrapper._obj_
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        wrapper._attr_.__set__(obj, None)

def iter2dict(iter):
    d = {}
    for item in iter:
        d[item] = d.get(item, 0) + 1
    return d

class PropagatedMultiset(object):
    __slots__ = [ '_obj_', '_parent_', '_attr_', '_items_' ]
    @cut_traceback
    def __init__(pset, parent, attr, items):
        pset._obj_ = parent._obj_
        pset._parent_ = parent
        pset._attr_ = attr
        pset._items_ = iter2dict(items)
    @cut_traceback
    def distinct(pset):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        return pset._items_.copy()
    @cut_traceback
    def __repr__(pset):
        if pset._obj_._cache_.is_alive:
            size = sum(pset._items_.itervalues())
            if size == 1: size_str = ' (1 item)'
            else: size_str = ' (%d items)' % size
        else: size_str = ''
        path = []
        wrapper = pset
        while wrapper is not None:
            path.append(wrapper._attr_.name)
            wrapper = wrapper._parent_
        return '<%s.%s%s>' % (pset._obj_, '.'.join(reversed(path)), size_str)
    @cut_traceback
    def __str__(pset):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        return str(pset._items_)
    @cut_traceback
    def __nonzero__(pset):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        return bool(pset._items_)
    @cut_traceback
    def __len__(pset):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        return sum(pset._items_.values())
    @cut_traceback
    def __iter__(pset):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        for item, count in pset._items_.iteritems():
            for i in range(count): yield item
    @cut_traceback
    def __eq__(pset, other):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if isinstance(other, PropagatedMultiset):
            return pset._items_ == other._items_
        if isinstance(other, dict):
            return pset._items_ == other
        if hasattr(other, 'keys'):
            return pset._items_ == dict(other)
        return pset._items_ == iter2dict(other)
    @cut_traceback
    def __ne__(pset, other):
        return not pset.__eq__(other)
    @cut_traceback
    def __contains__(pset, item):
        if not pset._obj_._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        return item in pset._items_

##class List(Collection): pass
##class Dict(Collection): pass
##class Relation(Collection): pass

class EntityIter(object):
    def __init__(self, entity):
        self.entity = entity
    def next(self):
        raise TranslationError('Use select(...) function to iterate over entity')

next_entity_id = count(1).next
next_new_instance_id = count(1).next

select_re = re.compile(r'select\b', re.IGNORECASE)
lambda_re = re.compile(r'lambda\b')

class EntityMeta(type):
    def __setattr__(entity, name, val):
        if name.startswith('_') and name.endswith('_'):
            type.__setattr__(entity, name, val)
        else: raise NotImplementedError
    def __new__(meta, name, bases, cls_dict):
        if 'Entity' in globals():
            if '__slots__' in cls_dict: raise TypeError('Entity classes cannot contain __slots__ variable')
            cls_dict['__slots__'] = ()
        return super(EntityMeta, meta).__new__(meta, name, bases, cls_dict)
    @cut_traceback
    def __init__(entity, name, bases, cls_dict):
        super(EntityMeta, entity).__init__(name, bases, cls_dict)
        entity._database_ = None
        if name == 'Entity': return

        databases = set()
        for base_class in bases:
            if isinstance(base_class, EntityMeta):
                database = base_class._database_
                if database is None: raise ERDiagramError('Base Entity does not belong to any database')
                databases.add(database)
        if not databases: assert False
        elif len(databases) > 1: raise ERDiagramError(
            'With multiple inheritance of entities, all entities must belong to the same database')
        database = databases.pop()

        if entity.__name__ in database.entities:
            raise ERDiagramError('Entity %s already exists' % entity.__name__)
        assert entity.__name__ not in database.__dict__

        entity._id_ = next_entity_id()
        direct_bases = [ c for c in entity.__bases__ if issubclass(c, Entity) and c.__name__ != 'Entity' ]
        entity._direct_bases_ = direct_bases
        all_bases = entity._all_bases_ = set()
        entity._subclasses_ = set()
        for base in direct_bases:
            all_bases.update(base._all_bases_)
            all_bases.add(base)
        for base in all_bases:
            base._subclasses_.add(entity)
        if direct_bases:
            roots = set(base._root_ for base in direct_bases)
            if len(roots) > 1: raise ERDiagramError(
                'With multiple inheritance of entities, inheritance graph must be diamond-like')
            root = entity._root_ = roots.pop()
            if root._discriminator_attr_ is None:
                assert root._discriminator_ is None
                Discriminator.create_default_attr(root)
        else:
            entity._root_ = entity
            entity._discriminator_attr_ = None

        base_attrs = []
        base_attrs_dict = {}
        for base in direct_bases:
            for a in base._attrs_:
                prev = base_attrs_dict.get(a.name)
                if prev is None:
                    base_attrs_dict[a.name] = a
                    base_attrs.append(a)
                elif prev is not a: raise ERDiagramError(
                    'Attribute "%s" clashes with attribute "%s" in derived entity "%s"'
                    % (prev, a, entity.__name__))
        entity._base_attrs_ = base_attrs

        new_attrs = []
        for name, attr in entity.__dict__.items():
            if name in base_attrs_dict: raise ERDiagramError("Name '%s' hides base attribute %s" % (name,base_attrs_dict[name]))
            if not isinstance(attr, Attribute): continue
            if name.startswith('_') and name.endswith('_'): raise ERDiagramError(
                'Attribute name cannot both start and end with underscore. Got: %s' % name)
            if attr.entity is not None: raise ERDiagramError(
                'Duplicate use of attribute %s in entity %s' % (attr, entity.__name__))
            attr._init_(entity, name)
            new_attrs.append(attr)
        new_attrs.sort(key=attrgetter('id'))

        keys = entity.__dict__.get('_keys_', {})
        for key in keys:
            for attr in key:
                assert isinstance(attr, Attribute) and not attr.is_collection
                if attr.entity is not entity: raise ERDiagramError(
                    'Invalid use of attribute %s in entity %s' % (attr, entity.__name__))
        primary_keys = set(key for key, is_pk in keys.items() if is_pk)
        if direct_bases:
            if primary_keys: raise ERDiagramError('Primary key cannot be redefined in derived classes')
            for base in direct_bases:
                keys[base._pk_attrs_] = True
                for key in base._keys_: keys[key] = False
            primary_keys = set(key for key, is_pk in keys.items() if is_pk)

        if len(primary_keys) > 1: raise ERDiagramError('Only one primary key can be defined in each entity class')
        elif not primary_keys:
            if hasattr(entity, 'id'): raise ERDiagramError(
                "Cannot create primary key for %s automatically because name 'id' is alredy in use" % entity.__name__)
            _keys_ = {}
            attr = PrimaryKey(int, auto=True) # Side effect: modifies _keys_ local variable
            attr._init_(entity, 'id')
            type.__setattr__(entity, 'id', attr)  # entity.id = attr
            new_attrs.insert(0, attr)
            key, is_pk = _keys_.popitem()
            keys[key] = True
            pk_attrs = key
        else: pk_attrs = primary_keys.pop()
        for i, attr in enumerate(pk_attrs): attr.pk_offset = i
        entity._pk_columns_ = None
        entity._pk_attrs_ = pk_attrs
        entity._pk_is_composite_ = len(pk_attrs) > 1
        entity._pk_ = len(pk_attrs) > 1 and pk_attrs or pk_attrs[0]
        entity._keys_ = [ key for key, is_pk in keys.items() if not is_pk ]
        entity._simple_keys_ = [ key[0] for key in entity._keys_ if len(key) == 1 ]
        entity._composite_keys_ = [ key for key in entity._keys_ if len(key) > 1 ]

        entity._new_attrs_ = new_attrs
        entity._attrs_ = base_attrs + new_attrs
        entity._adict_ = dict((attr.name, attr) for attr in entity._attrs_)
        entity._subclass_attrs_ = set()
        for base in entity._all_bases_:
            base._subclass_attrs_.update(new_attrs)

        entity._bits_ = {}
        next_offset = count().next
        all_bits = 0
        for attr in entity._attrs_:
            if attr.is_collection or attr.is_discriminator or attr.pk_offset is not None: continue
            next_bit = 1 << next_offset()
            entity._bits_[attr] = next_bit
            all_bits |= next_bit
        entity._all_bits_ = all_bits

        try: table_name = entity.__dict__['_table_']
        except KeyError: entity._table_ = None
        else:
            if not isinstance(table_name, basestring):
                if not isinstance(table_name, (list, tuple)): raise TypeError(
                    '%s._table_ property must be a string. Got: %r' % (entity.__name__, table_name))
                for name_part in table_name:
                    if not isinstance(name_part, basestring):raise TypeError(
                        'Each part of table name must be a string. Got: %r' % name_part)
                entity._table_ = table_name = tuple(table_name)

        entity._database_ = database
        database.entities[entity.__name__] = entity
        setattr(database, entity.__name__, entity)
        entity._link_reverse_attrs_()

        entity._cached_create_sql_ = None
        entity._cached_create_sql_auto_pk_ = None
        entity._cached_delete_sql_ = None
        entity._find_sql_cache_ = {}
        entity._batchload_sql_cache_ = {}
        entity._update_sql_cache_ = {}
        entity._lock_sql_cache_ = {}

        entity._propagation_mixin_ = None
        entity._set_wrapper_subclass_ = None
        entity._propagated_set_subclass_ = None

        if '_discriminator_' not in entity.__dict__:
            entity._discriminator_ = None
        if entity._discriminator_ and not entity._discriminator_attr_:
            Discriminator.create_default_attr(entity)
        if entity._discriminator_attr_:
            entity._discriminator_attr_.process_entity_inheritance(entity)
    def _link_reverse_attrs_(entity):
        database = entity._database_
        unmapped_attrs = database._unmapped_attrs.pop(entity.__name__, set())
        for attr in entity._new_attrs_:
            py_type = attr.py_type
            if isinstance(py_type, basestring):
                entity2 = database.entities.get(py_type)
                if entity2 is None:
                    database._unmapped_attrs.setdefault(py_type, set()).add(attr)
                    continue
                attr.py_type = py_type = entity2
            elif not issubclass(py_type, Entity): continue
            
            entity2 = py_type
            if entity2._database_ is not database:
                raise ERDiagramError('Interrelated entities must belong to same database. '
                                   'Entities %s and %s belongs to different databases'
                                   % (entity.__name__, entity2.__name__))
            reverse = attr.reverse
            if isinstance(reverse, basestring):
                attr2 = getattr(entity2, reverse, None)
                if attr2 is None: raise ERDiagramError('Reverse attribute %s.%s not found' % (entity2.__name__, reverse))
            elif isinstance(reverse, Attribute):
                attr2 = reverse
                if attr2.entity is not entity2: raise ERDiagramError('Incorrect reverse attribute %s used in %s' % (attr2, attr)) ###
            elif reverse is not None: raise ERDiagramError("Value of 'reverse' option must be string. Got: %r" % type(reverse))
            else:
                candidates1 = []
                candidates2 = []
                for attr2 in entity2._new_attrs_:
                    if attr2.py_type not in (entity, entity.__name__): continue
                    reverse2 = attr2.reverse
                    if reverse2 in (attr, attr.name): candidates1.append(attr2)
                    elif not reverse2:
                        if attr2 is attr: continue
                        candidates2.append(attr2)
                msg = 'Ambiguous reverse attribute for %s'
                if len(candidates1) > 1: raise ERDiagramError(msg % attr)
                elif len(candidates1) == 1: attr2 = candidates1[0]
                elif len(candidates2) > 1: raise ERDiagramError(msg % attr)
                elif len(candidates2) == 1: attr2 = candidates2[0]
                else: raise ERDiagramError('Reverse attribute for %s not found' % attr)

            type2 = attr2.py_type
            msg = 'Inconsistent reverse attributes %s and %s'
            if isinstance(type2, basestring):
                if type2 != entity.__name__: raise ERDiagramError(msg % (attr, attr2))
                attr2.py_type = entity
            elif type2 != entity: raise ERDiagramError(msg % (attr, attr2))
            reverse2 = attr2.reverse
            if reverse2 not in (None, attr, attr.name): raise ERDiagramError(msg % (attr,attr2))

            if attr.is_required and attr2.is_required: raise ERDiagramError(
                "At least one attribute of one-to-one relationship %s - %s must be optional" % (attr, attr2))

            attr.reverse = attr2
            attr2.reverse = attr
            unmapped_attrs.discard(attr2)          
        for attr in unmapped_attrs:
            raise ERDiagramError('Reverse attribute for %s.%s was not found' % (attr.entity.__name__, attr.name))        
    def _get_pk_columns_(entity):
        if entity._pk_columns_ is not None: return entity._pk_columns_
        pk_columns = []
        pk_converters = []
        pk_paths = []
        for attr in entity._pk_attrs_:
            attr_columns = attr.get_columns()
            attr_col_paths = attr.col_paths
            attr.pk_columns_offset = len(pk_columns)
            pk_columns.extend(attr_columns)
            pk_converters.extend(attr.converters)
            pk_paths.extend(attr_col_paths)
        entity._pk_columns_ = pk_columns
        entity._pk_converters_ = pk_converters
        entity._pk_nones_ = (None,) * len(pk_columns)
        entity._pk_paths_ = pk_paths
        return pk_columns
    def __iter__(entity):
        return EntityIter(entity)
    def _normalize_args_(entity, keyargs, setdefault=False):
        avdict = {}
        if setdefault:
            for name in ifilterfalse(entity._adict_.__contains__, keyargs):
                raise TypeError('Unknown attribute %r' % name)
            for attr in entity._attrs_:
                val = keyargs.get(attr.name, DEFAULT)
                avdict[attr] = attr.check(val, None, entity, from_db=False)
        else:
            get = entity._adict_.get 
            for name, val in keyargs.items():
                attr = get(name)
                if attr is None: raise TypeError('Unknown attribute %r' % name)
                avdict[attr] = attr.check(val, None, entity, from_db=False)
        if entity._pk_is_composite_:
            pkval = map(avdict.get, entity._pk_attrs_)
            if None in pkval: pkval = None
            else: pkval = tuple(pkval)
        else: pkval = avdict.get(entity._pk_)
        return pkval, avdict        
    @cut_traceback
    def fetch(entity, *args, **keyargs):
        return entity._find_(None, args, keyargs)
    @cut_traceback
    def fetch_one(entity, *args, **keyargs):
        objects = entity._find_(1, args, keyargs)
        if not objects: return None
        if len(objects) > 1: raise MultipleObjectsFoundError(
            'Multiple objects were found. Use %s.fetch(...) to retrieve them' % entity.__name__)
        return objects[0]
    def _find_by_sql_(entity, max_fetch_count, sql, globals=None, locals=None, frame_depth=1):
        if not isinstance(sql, basestring): raise TypeError
        database = entity._database_
        cursor = database._execute(sql, globals, locals, frame_depth+3)

        col_names = [ column_info[0].upper() for column_info in cursor.description ]
        attr_offsets = {}
        used_columns = set()
        for attr in entity._attrs_:
            if attr.is_collection: continue
            if not attr.columns: continue
            offsets = []
            for column in attr.columns:
                try: offset = col_names.index(column.upper())
                except ValueError: break
                offsets.append(offset)
                used_columns.add(offset)
            else: attr_offsets[attr] = offsets
        if len(used_columns) < len(col_names):
            for i in range(len(col_names)):
                if i not in used_columns: raise NameError(
                    'Column %s does not belong to entity %s' % (cursor.description[i][0], entity.__name__))
        for attr in entity._pk_attrs_:
            if attr not in attr_offsets: raise ValueError(
                'Primary key attribue %s was not found in query result set' % attr)
        
        objects = entity._fetch_objects(cursor, attr_offsets, max_fetch_count)
        return objects
    @cut_traceback
    def __getitem__(entity, key):
        if type(key) is not tuple: key = (key,)
        if len(key) != len(entity._pk_attrs_): raise TypeError('Invalid count of attrs in primary key')
        keyargs = dict(izip(imap(attrgetter('name'), entity._pk_attrs_), key))
        objects = entity._find_(1, (), keyargs)
        if not objects: raise ObjectNotFound(entity, key)
        if len(objects) > 1: raise MultipleObjectsFoundError(
            'Multiple objects was found. Use %s.fetch(...) to retrieve them' % entity.__name__)
        return objects[0]
    @cut_traceback
    def query(entity, func):
        if isinstance(func, basestring):
            if not lambda_re.match(func): raise TypeError(
                'Lambda function or its text representation expected')
        elif not isinstance(func, types.FunctionType): raise TypeError
        globals = sys._getframe(2).f_globals
        locals = sys._getframe(2).f_locals
        return entity._query_from_lambda_(func, globals, locals)
    @cut_traceback
    def orderby(entity, *args):
        name = (''.join(letter for letter in entity.__name__ if letter.isupper())).lower() or entity.__name__[0]
        for_expr = ast.GenExprFor(ast.AssName(name, 'OP_ASSIGN'), ast.Name('.0'), [])
        inner_expr = ast.GenExprInner(ast.Name(name), [ for_expr ])
        query = Query(None, inner_expr, set(['.0']), {}, { '.0' : entity })
        return query.orderby(*args)
    def _find_(entity, max_fetch_count, args, keyargs):
        if entity._database_.schema is None:
            raise ERDiagramError('Mapping is not generated for entity %r' % entity.__name__)
        
        if args:
            first_arg = args[0]
            is_string = isinstance(first_arg, basestring)

            msg = 'Positional argument must be lambda function or SQL select command. Got: %r'
            if is_string:
                if select_re.match(first_arg):
                    return entity._find_by_sql_(max_fetch_count, *args, **keyargs)
                elif not lambda_re.match(first_arg): raise TypeError(msg % first_arg)
            elif not isinstance(first_arg, types.FunctionType): raise TypeError(msg % first_arg)
            if len(args) > 1: raise TypeError('Only one positional argument expected')
            if keyargs: raise TypeError('No keyword arguments expected')

            globals = sys._getframe(3).f_globals
            locals = sys._getframe(3).f_locals
            query = entity._query_from_lambda_(first_arg, globals, locals)
            return query.fetch()

        pkval, avdict = entity._normalize_args_(keyargs, False)
        for attr in avdict:
            if attr.is_collection: raise TypeError(
                'Collection attribute %s.%s cannot be specified as search criteria' % (attr.entity.__name__, attr.name))
        try:
            objects = entity._find_in_cache_(pkval, avdict)
        except KeyError:  # not found in cache, can exist in db
            objects = entity._find_in_db_(avdict, max_fetch_count)
        return objects
    def _find_in_cache_(entity, pkval, avdict):
        cache = entity._get_cache_()
        obj = None
        if pkval is not None:
            index = cache.indexes.get(entity._pk_)
            if index is not None: obj = index.get(pkval)
        if obj is None:
            for attr in ifilter(avdict.__contains__, entity._simple_keys_):
                index = cache.indexes.get(attr)
                if index is None: continue
                val = avdict[attr]
                obj = index.get(val)
                if obj is not None: break
        if obj is None:
            NOT_FOUND = object()
            for attrs in entity._composite_keys_:
                vals = tuple(avdict.get(attr, NOT_FOUND) for attr in attrs)
                if NOT_FOUND in vals: continue
                index = cache.indexes.get(attrs)
                if index is None: continue
                obj = index.get(vals)
                if obj is not None: break
        if obj is None:
            for attr, val in avdict.iteritems():
                if val is None: continue
                reverse = attr.reverse
                if reverse and not reverse.is_collection:
                    obj = reverse.__get__(val)
                    break
        if obj is None:
            for attr, val in avdict.iteritems():
                if isinstance(val, Entity) and val._pkval_ is None:
                    reverse = attr.reverse
                    if not reverse.is_collection:
                        obj = reverse.__get__(val)
                        if obj is None: return []
                    elif isinstance(reverse, Set):
                        filtered_objects = []
                        for obj in reverse.__get__(val):
                            for attr, val in avdict.iteritems():
                                if val != attr.get(obj): break
                            else: filtered_objects.append(obj)
                        filtered_objects.sort(key=entity._get_raw_pkval_)
                        return filtered_objects
                    else: raise NotImplementedError
        if obj is not None:
            if obj._status_ == 'deleted': return []
            for attr, val in avdict.iteritems():
                if val != attr.__get__(obj):
                    return []
            return [ obj ]
        raise KeyError  # not found in cache, can exist in db
    def _find_in_db_(entity, avdict, max_fetch_count=None):
        if max_fetch_count is None: max_fetch_count = options.MAX_FETCH_COUNT
        database = entity._database_
        query_attrs = tuple((attr, value is None) for attr, value in sorted(avdict.iteritems()))
        single_row = (max_fetch_count == 1)
        sql, extractor, adapter, attr_offsets = entity._construct_sql_(query_attrs, order_by_pk=not single_row)
        value_dict = extractor(avdict)
        arguments = adapter(value_dict)
        cursor = database._exec_sql(sql, arguments)
        objects = entity._fetch_objects(cursor, attr_offsets, max_fetch_count)
        return objects
    def _construct_select_clause_(entity, alias=None, distinct=False):
        attr_offsets = {}
        if distinct: select_list = [ DISTINCT ]
        else: select_list = [ ALL ]
        for attr in chain(entity._attrs_, entity._subclass_attrs_):
            if attr.is_collection: continue
            if not attr.columns: continue
            attr_offsets[attr] = offsets = []
            for column in attr.columns:
                offsets.append(len(select_list) - 1)
                select_list.append([ COLUMN, alias, column ])
        return select_list, attr_offsets
    def _construct_batchload_sql_(entity, batch_size, attr=None):
        query_key = batch_size, attr
        cached_sql = entity._batchload_sql_cache_.get(query_key)
        if cached_sql is not None: return cached_sql
        table_name = entity._table_
        select_list, attr_offsets = entity._construct_select_clause_()
        from_list = [ FROM, [ None, TABLE, table_name ]]
        if attr is None:
            columns = entity._pk_columns_
            converters = entity._pk_converters_
        else:
            columns = attr.columns
            converters = attr.converters
        if batch_size == 1:
            criteria_list = [ [ EQ, [ COLUMN, None, column ], [ PARAM, (0, i), converter ] ]
                              for i, (column, converter) in enumerate(izip(columns, converters)) ]
        elif len(columns) == 1:
            converter = converters[0]
            criteria_list = [ [ IN, [ COLUMN, None, columns[0] ],
                                   [ [ PARAM, (i, 0), converter ] for i in xrange(batch_size) ] ] ]
        elif entity._database_.provider.translator_cls.row_value_syntax:
            criteria_list = [ [ IN, [ ROW ] + [ [ COLUMN, None, column ] for column in columns ],
                                    [ [ ROW ] + [ [ PARAM, (i, j), converter ] for j, converter in enumerate(converters) ]
                                     for i in xrange(batch_size) ] ] ]
        else:
            pairs = zip(columns, converters)
            criteria_list = [ [ OR ] + [ [ AND ] + [ [ EQ, [ COLUMN, None, column ], [ PARAM, (i, j), converter ] ]
                                                     for j, (column, converter) in enumerate(pairs) ]
                                         for i in xrange(batch_size) ] ]
        sql_ast = [ SELECT, select_list, from_list, [ WHERE ] + criteria_list ]
        database = entity._database_
        sql, adapter = database._ast2sql(sql_ast)
        cached_sql = sql, adapter, attr_offsets
        entity._batchload_sql_cache_[query_key] = cached_sql
        return cached_sql
    def _construct_sql_(entity, query_attrs, order_by_pk=False):
        query_key = query_attrs, order_by_pk
        cached_sql = entity._find_sql_cache_.get(query_key)
        if cached_sql is not None: return cached_sql
        table_name = entity._table_
        select_list, attr_offsets = entity._construct_select_clause_()
        from_list = [ FROM, [ None, TABLE, table_name ]]
        where_list = [ WHERE ]
        values = []
        extractors = {}

        discr_attr = entity._discriminator_attr_
        if discr_attr is not None and discr_attr not in query_attrs:
            code2cls = discr_attr.code2cls
            discr_values = [ [ VALUE, cls._discriminator_ ] for cls in entity._subclasses_ ]
            discr_values.append([ VALUE, entity._discriminator_])

            where_list.append([ IN, [ COLUMN, None, discr_attr.column ], discr_values ])
        
        for attr, attr_is_none in query_attrs:
            if not attr.reverse:
                if not attr_is_none:
                    assert len(attr.converters) == 1
                    where_list.append([EQ, [COLUMN, None, attr.column], [ PARAM, attr.name, attr.converters[0] ]])
                    extractors[attr.name] = lambda avdict, attr=attr: avdict[attr]
                else: where_list.append([IS_NULL, [COLUMN, None, attr.column]])
            elif not attr.columns: raise NotImplementedError
            else:
                attr_entity = attr.py_type
                assert attr_entity == attr.reverse.entity
                if len(attr.columns) == 1:
                    if not attr_is_none:
                        assert len(attr.converters) == 1
                        where_list.append([EQ, [COLUMN, None, attr.column], [ PARAM, attr.name, attr.converters[0] ]])
                        extractors[attr.name] = lambda avdict, attr=attr: avdict[attr]._get_raw_pkval_()[0]
                    else: where_list.append([IS_NULL, [COLUMN, None, attr.column]])
                elif not attr_is_none:
                    for i, (column, converter) in enumerate(zip(attr.columns, attr_entity._pk_converters_)):
                        param_name = '%s-%d' % (attr.name, i+1)
                        where_list.append([EQ, [COLUMN, None, column], [ PARAM, param_name, converter ]])
                        extractors[param_name] = lambda avdict, attr=attr, i=i: avdict[attr]._get_raw_pkval_()[i]
                else:
                    for column in attr.columns:
                        where_list.append([IS_NULL, [COLUMN, None, column]])

        sql_ast = [ SELECT, select_list, from_list, where_list ]
        if order_by_pk: sql_ast.append([ ORDER_BY ] + [ ([COLUMN, None, column], ASC) for column in entity._pk_columns_ ])
        database = entity._database_
        sql, adapter = database._ast2sql(sql_ast)
        def extractor(avdict):
            param_dict = {}
            for param, extractor in extractors.iteritems():
                param_dict[param] = extractor(avdict)
            return param_dict
        cached_sql = sql, extractor, adapter, attr_offsets
        entity._find_sql_cache_[query_key] = cached_sql
        return cached_sql
    def _fetch_objects(entity, cursor, attr_offsets, max_fetch_count=None):
        if max_fetch_count is None: max_fetch_count = options.MAX_FETCH_COUNT
        if max_fetch_count is not None:
            rows = cursor.fetchmany(max_fetch_count + 1)
            if len(rows) == max_fetch_count + 1:
                if max_fetch_count == 1: raise MultipleObjectsFoundError(
                    'Multiple objects were found. Use %s.fetch(...) to retrieve them' % entity.__name__)
                raise TooManyObjectsFoundError(
                    'Found more then pony.options.MAX_FETCH_COUNT=%d objects' % options.MAX_FETCH_COUNT)
        else: rows = cursor.fetchall()
        objects = []
        for row in rows:
            real_entity_subclass, pkval, avdict = entity._parse_row_(row, attr_offsets)
            obj = real_entity_subclass._new_(pkval, 'loaded')
            if obj._status_ in ('deleted', 'cancelled'): continue
            obj._db_set_(avdict)
            objects.append(obj)
        return objects
    def _parse_row_(entity, row, attr_offsets):
        discr_attr = entity._discriminator_attr_
        if not discr_attr: real_entity_subclass = entity
        else:
            discr_offset = attr_offsets[discr_attr][0]
            discr_value = discr_attr.check(row[discr_offset], None, entity, from_db=True)
            real_entity_subclass = discr_attr.code2cls[discr_value]

        avdict = {}
        for attr in real_entity_subclass._attrs_:
            offsets = attr_offsets.get(attr)
            if offsets is None or attr.is_discriminator: continue
            assert len(attr.columns) == len(offsets)
            if not attr.reverse:
                if len(offsets) > 1: raise NotImplementedError
                offset = offsets[0]
                val = attr.check(row[offset], None, entity, from_db=True)
            else:
                vals = map(row.__getitem__, offsets)
                if None in vals:
                    assert len(set(vals)) == 1
                    val = None
                else: val = attr.py_type._get_by_raw_pkval_(vals)
            avdict[attr] = val
        if not entity._pk_is_composite_: pkval = avdict.pop(entity._pk_, None)            
        else: pkval = tuple(avdict.pop(attr, None) for attr in entity._pk_attrs_)
        return real_entity_subclass, pkval, avdict
    def _query_from_lambda_(entity, func, globals, locals):
        if not isinstance(func, basestring):
            names, argsname, keyargsname, defaults = inspect.getargspec(func)
            if len(names) > 1: raise TypeError
            if argsname or keyargsname: raise TypeError
            if defaults: raise TypeError
            code = func.func_code
            name = names[0]
            cond_expr, external_names = decompile(func)
        else:
            module_node = parse(func)
            if not isinstance(module_node, ast.Module): raise TypeError
            stmt_node = module_node.node
            if not isinstance(stmt_node, ast.Stmt) or len(stmt_node.nodes) != 1: raise TypeError
            discard_node = stmt_node.nodes[0]
            if not isinstance(discard_node, ast.Discard): raise TypeError
            lambda_expr = discard_node.expr
            if not isinstance(lambda_expr, ast.Lambda): raise TypeError
            if len(lambda_expr.argnames) != 1: raise TypeError
            if lambda_expr.varargs: raise TypeError
            if lambda_expr.kwargs: raise TypeError
            if lambda_expr.defaults: raise TypeError
            code = func
            name = lambda_expr.argnames[0]            
            cond_expr = lambda_expr.code
            external_names = set()
            def collect_names(tree):
                for child in tree.getChildNodes():
                    if isinstance(child, ast.Name): external_names.add(child.name)
                    else: collect_names(child)
            collect_names(cond_expr)

        external_names.discard(name)
        external_names.add('.0')
        if_expr = ast.GenExprIf(cond_expr)
        for_expr = ast.GenExprFor(ast.AssName(name, 'OP_ASSIGN'), ast.Name('.0'), [ if_expr ])
        inner_expr = ast.GenExprInner(ast.Name(name), [ for_expr ])
        locals = locals.copy()
        assert '.0' not in locals
        locals['.0'] = entity
        return Query(code, inner_expr, external_names, globals, locals)
    def _get_cache_(entity):
        database = entity._database_
        if database is None: raise TransactionError
        return database._get_cache()
    def _new_(entity, pkval, status, undo_funcs=None):
        cache = entity._get_cache_()
        index = cache.indexes.setdefault(entity._pk_, {})
        if pkval is None: obj = None
        else: obj = index.get(pkval)
        if obj is None: pass
        elif status == 'created':
            if entity._pk_is_composite_: pkval = ', '.join(str(item) for item in pkval)
            raise CacheIndexError('Cannot create %s: instance with primary key %s already exists'
                             % (obj.__class__.__name__, pkval))                
        else: return obj
        obj = object.__new__(entity)
        obj._dbvals_ = {}
        obj._vals_ = {}
        obj._cache_ = cache
        obj._status_ = status
        obj._pkval_ = pkval
        if pkval is not None:
            index[pkval] = obj
            obj._newid_ = None
        else: obj._newid_ = next_new_instance_id()
        if obj._pk_is_composite_: pairs = zip(entity._pk_attrs_, pkval)
        else: pairs = ((entity._pk_, pkval),)
        if status == 'loaded':
            assert undo_funcs is None
            obj._rbits_ = obj._wbits_ = 0
            for attr, val in pairs:
                obj._vals_[attr.name] = val
                if attr.reverse: attr.db_update_reverse(obj, NOT_LOADED, val)
            seeds = cache.seeds.setdefault(entity._pk_, set())
            seeds.add(obj)
        elif status == 'created':
            assert undo_funcs is not None
            obj._rbits_ = obj._wbits_ = None
            for attr, val in pairs:
                obj._vals_[attr.name] = val
                if attr.reverse: attr.update_reverse(obj, NOT_LOADED, val, undo_funcs)
        else: assert False
        return obj
    def _get_by_raw_pkval_(entity, raw_pkval):
        i = 0
        pkval = []
        for attr in entity._pk_attrs_:
            if attr.column is not None:
                val = raw_pkval[i]
                i += 1
                if not attr.reverse: val = attr.check(val, None, entity, from_db=True)
                else: val = attr.py_type._get_by_raw_pkval_((val,))
            else:
                if not attr.reverse: raise NotImplementedError
                vals = raw_pkval[i:i+len(attr.columns)]
                val = attr.py_type._get_by_raw_pkval_(vals)
            pkval.append(val)
        if not entity._pk_is_composite_: pkval = pkval[0]
        else: pkval = tuple(pkval)
        obj = entity._new_(pkval, 'loaded')
        assert obj._status_ not in ('deleted', 'cancelled')
        return obj
    def _get_propagation_mixin_(entity):
        mixin = entity._propagation_mixin_
        if mixin is not None: return mixin
        cls_dict = { '_entity_' : entity }
        for attr in entity._attrs_:
            if not attr.reverse:
                def fget(wrapper, attr=attr):
                    items = [ attr.__get__(item) for item in wrapper ]
                    return PropagatedMultiset(wrapper, attr, items)
            elif not attr.is_collection:
                def fget(wrapper, attr=attr):
                    items = [ attr.__get__(item) for item in wrapper ]
                    rentity = attr.py_type
                    cls = rentity._get_propagated_set_subclass_()
                    return cls(wrapper, attr, items)
            else:
                def fget(wrapper, attr=attr):
                    items = [ subitem for item in wrapper
                                      for subitem in attr.__get__(item) ]
                    rentity = attr.py_type
                    cls = rentity._get_propagated_set_subclass_()
                    return cls(wrapper, attr, items)
            cls_dict[attr.name] = property(fget)
        result_cls_name = entity.__name__ + 'SetMixin'
        result_cls = type(result_cls_name, (object,), cls_dict)
        entity._propagation_mixin_ = result_cls
        return result_cls
    def _get_propagated_set_subclass_(entity):
        result_cls = entity._propagated_set_subclass_
        if result_cls is None:
            mixin = entity._get_propagation_mixin_()
            cls_name = entity.__name__ + 'PropagatedMultiset'
            result_cls = type(cls_name, (PropagatedMultiset, mixin), {})
            entity._propagated_set_subclass_ = result_cls
        return result_cls
    def _get_set_wrapper_subclass_(entity):
        result_cls = entity._set_wrapper_subclass_
        if result_cls is None:
            mixin = entity._get_propagation_mixin_()
            cls_name = entity.__name__ + 'SetWrapper'
            result_cls = type(cls_name, (SetWrapper, mixin), {})
            entity._set_wrapper_subclass_ = result_cls
        return result_cls
    def describe(entity):
        result = []
        parents = ','.join(cls.__name__ for cls in entity.__bases__)
        result.append('class %s(%s):' % (entity.__name__, parents))
        if entity._base_attrs_:
            result.append('# inherited attrs')
            result.extend(attr.describe() for attr in entity._base_attrs_)
            result.append('# attrs introduced in %s' % entity.__name__)
        result.extend(attr.describe() for attr in entity._new_attrs_)
        return '\n    '.join(result)
    
class Entity(object):
    __metaclass__ = EntityMeta
    __slots__ = '_cache_', '_status_', '_pkval_', '_newid_', '_dbvals_', '_vals_', '_rbits_', '_wbits_', '__weakref__'
    def __new__(entity, **keyargs):
        if entity._database_.schema is None:
            raise ERDiagramError('Mapping is not generated for entity %r' % entity.__name__)

        pkval, avdict = entity._normalize_args_(keyargs, True)
        undo_funcs = []
        cache = entity._get_cache_()
        indexes = {}
        for attr in entity._simple_keys_:
            val = avdict[attr]
            if val in cache.indexes.setdefault(attr, {}): raise CacheIndexError(
                'Cannot create %s: value %s for key %s already exists' % (entity.__name__, val, attr.name))
            indexes[attr] = val
        for attrs in entity._composite_keys_:
            vals = tuple(map(avdict.__getitem__, attrs))
            if vals in cache.indexes.setdefault(attrs, {}):
                attr_names = ', '.join(attr.name for attr in attrs)
                raise CacheIndexError('Cannot create %s: value %s for composite key (%s) already exists'
                                 % (obj.__class__.__name__, vals, attr_names))
            indexes[attrs] = vals
        try:
            obj = entity._new_(pkval, 'created', undo_funcs)
            for attr, val in avdict.iteritems():
                if attr.pk_offset is not None: continue
                elif not attr.is_collection:
                    obj._vals_[attr.name] = val
                    if attr.reverse: attr.update_reverse(obj, None, val, undo_funcs)
                else: attr.__set__(obj, val, undo_funcs)
        except:
            for undo_func in reversed(undo_funcs): undo_func()
            raise
        if pkval is not None:
            cache.indexes[entity._pk_][pkval] = obj
        for key, vals in indexes.iteritems():
            cache.indexes[key][vals] = obj
        cache.created.add(obj)
        cache.to_be_checked.append(obj)
        return obj
    def _get_raw_pkval_(obj):
        pkval = obj._pkval_
        if not obj._pk_is_composite_:
            if not obj.__class__._pk_.reverse: return (pkval,)
            else: return pkval._get_raw_pkval_()
        raw_pkval = []
        append = raw_pkval.append
        for attr, val in zip(obj._pk_attrs_, pkval):
            if not attr.reverse: append(val)
            else: raw_pkval += val._get_raw_pkval_()
        return tuple(raw_pkval)
    @cut_traceback
    def __repr__(obj):
        pkval = obj._pkval_
        if pkval is None: return '%s[new:%d]' % (obj.__class__.__name__, obj._newid_)
        if obj._pk_is_composite_: pkval = ','.join(map(repr, pkval))
        else: pkval = repr(pkval)
        return '%s[%s]' % (obj.__class__.__name__, pkval)
    def _load_(obj):
        cache = obj._cache_
        if not cache.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        entity = obj.__class__
        database = entity._database_
        seeds = cache.seeds[entity._pk_]
        max_batch_size = database.provider.max_params_count // len(entity._pk_columns_)
        objects = [ obj ]
        for seed in seeds:
            if len(objects) >= max_batch_size: break
            if seed is not obj: objects.append(seed)
        sql, adapter, attr_offsets = entity._construct_batchload_sql_(len(objects))
        value_dict = {}
        for i, obj in enumerate(objects):
            for j, val in enumerate(obj._get_raw_pkval_()): value_dict[i, j] = val
        arguments = adapter(value_dict)
        cursor = database._exec_sql(sql, arguments)
        objects = entity._fetch_objects(cursor, attr_offsets)
        if obj not in objects: raise UnrepeatableReadError('%s disappeared' % obj)
    def _db_set_(obj, avdict):
        assert obj._status_ not in ('created', 'deleted', 'cancelled')
        if not avdict: return
        get_val = obj._vals_.get
        get_dbval = obj._dbvals_.get
        rbits = obj._rbits_
        wbits = obj._wbits_
        for attr, new_dbval in avdict.items():
            assert attr.pk_offset is None
            old_dbval = get_dbval(attr.name, NOT_LOADED)

            if attr.py_type is float:
                if old_dbval is NOT_LOADED: pass
                elif attr.converters[0].equals(old_dbval, new_dbval):
                    del avdict[attr]
                    continue
            elif old_dbval == new_dbval:
                del avdict[attr]
                continue
            
            bit = obj._bits_[attr]
            if rbits & bit: raise UnrepeatableReadError(
                'Value of %s.%s for %s was updated outside of current transaction (was: %r, now: %r)'
                % (obj.__class__.__name__, attr.name, obj, old_dbval, new_dbval))

            if attr.reverse: attr.db_update_reverse(obj, old_dbval, new_dbval)
            if new_dbval is NOT_LOADED:
                obj._dbvals_.pop(attr.name, None)
                if wbits & bit: del avdict[attr]
                else: obj._vals_.pop(attr.name, None)
            else:
                obj._dbvals_[attr.name] = new_dbval
                if wbits & bit: del avdict[attr]
                else: obj._vals_[attr.name] = new_dbval

        NOT_FOUND = object()
        cache = obj._cache_
        assert cache.is_alive
        seeds = cache.seeds.setdefault(obj.__class__._pk_, set())
        seeds.discard(obj)
        for attr in obj._simple_keys_:
            new_dbval = avdict.get(attr, NOT_FOUND)
            if new_dbval is NOT_FOUND: continue
            old_val = get_val(attr.name, NOT_LOADED)
            if old_val == new_dbval: continue
            cache.db_update_simple_index(obj, attr, old_val, new_dbval)
        for attrs in obj._composite_keys_:
            for attr in attrs:
                if attr in avdict: break
            else: continue
            vals = [ get_val(a.name, NOT_LOADED) for a in attrs ]
            currents = tuple(vals)
            for i, attr in enumerate(attrs):
                new_dbval = avdict.get(attr, NOT_FOUND)
                if new_dbval is NOT_FOUND: continue
                vals[i] = new_dbval
            vals = tuple(vals)
            cache.db_update_composite_index(obj, attrs, currents, vals)
    def _delete_(obj, undo_funcs=None):
        is_recursive_call = undo_funcs is not None
        if not is_recursive_call: undo_funcs = []
        cache = obj._cache_
        status = obj._status_
        assert status not in ('deleted', 'cancelled')
        get_val = obj._vals_.get
        undo_list = []
        undo_dict = {}
        def undo_func():
            obj._status_ = status
            if status in ('loaded', 'saved'):
                to_be_checked = cache.to_be_checked
                if to_be_checked and to_be_checked[-1] is obj: to_be_checked.pop()
                assert obj not in to_be_checked
            obj._vals_.update((attr.name, val) for attr, val in undo_dict.iteritems())
            for index, old_key in undo_list: index[old_key] = obj
        undo_funcs.append(undo_func)
        try:
            for attr in obj._attrs_:
                reverse = attr.reverse
                if not reverse: continue
                if not attr.is_collection:
                    val = get_val(attr.name, NOT_LOADED)
                    if val is None: continue
                    if not reverse.is_collection:
                        if val is NOT_LOADED: val = attr.load(obj)
                        if val is None: continue
                        if reverse.is_required:
                            raise ConstraintError('Cannot delete %s: Attribute %s.%s for %s cannot be set to None'
                                                  % (obj, reverse.entity.__name__, reverse.name, val))
                        reverse.__set__(val, None, undo_funcs)
                    elif isinstance(reverse, Set):
                        if val is NOT_LOADED: pass
                        else: reverse.reverse_remove((val,), obj, undo_funcs)
                    else: raise NotImplementedError
                elif isinstance(attr, Set):
                    if reverse.is_required and attr.__get__(obj).__nonzero__(): raise ConstraintError(
                        'Cannot delete %s: Attribute %s.%s for associated objects cannot be set to None'
                        % (obj, reverse.entity.__name__, reverse.name))
                    attr.__set__(obj, (), undo_funcs)
                else: raise NotImplementedError

            for attr in obj._simple_keys_:
                val = get_val(attr.name, NOT_LOADED)
                if val is NOT_LOADED: continue
                if val is None and cache.ignore_none: continue
                index = cache.indexes.get(attr)
                if index is None: continue
                obj2 = index.pop(val)
                assert obj2 is obj
                undo_list.append((index, val))
                
            for attrs in obj._composite_keys_:
                vals = tuple(get_val(a.name, NOT_LOADED) for a in attrs)
                if NOT_LOADED in vals: continue
                if cache.ignore_none and None in vals: continue
                index = cache.indexes.get(attrs)
                if index is None: continue
                obj2 = index.pop(vals)
                assert obj2 is obj
                undo_list.append((index, vals))

            if status == 'created':
                obj._status_ = 'cancelled'
                assert obj in cache.created
                cache.created.remove(obj)
            else:
                if status == 'updated': cache.updated.remove(obj)
                elif status in ('loaded', 'saved'): cache.to_be_checked.append(obj)
                else: assert status == 'locked'
                obj._status_ = 'deleted'
                cache.deleted.add(obj)
            for attr in obj._attrs_:
                if attr.pk_offset is None:
                    val = obj._vals_.pop(attr.name, NOT_LOADED)
                    if val is NOT_LOADED: continue
                    undo_dict[attr] = val
        except:
            if not is_recursive_call:
                for undo_func in reversed(undo_funcs): undo_func()
            raise
    @cut_traceback
    def delete(obj):
        if not obj._cache_.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        obj._delete_()
    @cut_traceback
    def set(obj, **keyargs):
        cache = obj._cache_
        if not cache.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ in ('deleted', 'cancelled'): raise OperationWithDeletedObjectError('%s was deleted' % obj)
        avdict, collection_avdict = obj._keyargs_to_avdicts_(keyargs)
        status = obj._status_
        wbits = obj._wbits_
        get_val = obj._vals_.get
        if avdict:
            for attr in avdict:
                old_val = get_val(attr.name, NOT_LOADED)
                if old_val is NOT_LOADED and attr.reverse and not attr.reverse.is_collection:
                    attr.load(obj)
            if wbits is not None:
                new_wbits = wbits
                for attr in avdict: new_wbits |= obj._bits_[attr]
                obj._wbits_ = new_wbits
                if status != 'updated':
                    obj._status_ = 'updated'
                    cache.updated.add(obj)
                    if status in ('loaded', 'saved'): cache.to_be_checked.append(obj)
                    else: assert status == 'locked'
            if not collection_avdict:
                for attr in avdict:
                    if attr.reverse or attr.is_indexed: break
                else:
                    obj._vals_.update((attr.name, new_val) for attr, new_val in avdict.iteritems())
                    return
        undo_funcs = []
        undo = []
        def undo_func():
            obj._status_ = status
            obj._wbits_ = wbits
            if wbits == 0: cache.updated.remove(obj)
            if status in ('loaded', 'saved'):
                to_be_checked = cache.to_be_checked
                if to_be_checked and to_be_checked[-1] is obj: to_be_checked.pop()
                assert obj not in to_be_checked
            for index, old_key, new_key in undo:
                if new_key is NO_UNDO_NEEDED: pass
                else: del index[new_key]
                if old_key is NO_UNDO_NEEDED: pass
                else: index[old_key] = obj
        NOT_FOUND = object()
        try:
            for attr in obj._simple_keys_:
                new_val = avdict.get(attr, NOT_FOUND)
                if new_val is NOT_FOUND: continue
                old_val = get_val(attr.name, NOT_LOADED)
                if old_val == new_val: continue
                cache.update_simple_index(obj, attr, old_val, new_val, undo)
            for attrs in obj._composite_keys_:
                for attr in attrs:
                    if attr in avdict: break
                else: continue
                vals = [ get_val(a.name, NOT_LOADED) for a in attrs ]
                currents = tuple(vals)
                for i, attr in enumerate(attrs):
                    new_val = avdict.get(attr, NOT_FOUND)
                    if new_val is NOT_FOUND: continue
                    vals[i] = new_val
                vals = tuple(vals)
                cache.update_composite_index(obj, attrs, currents, vals, undo)
            for attr, new_val in avdict.iteritems():
                if not attr.reverse: continue
                old_val = get_val(attr.name, NOT_LOADED)
                attr.update_reverse(obj, old_val, new_val, undo_funcs)
            for attr, new_val in collection_avdict.iteritems():
                attr.__set__(obj, new_val, undo_funcs)
        except:
            for undo_func in undo_funcs: undo_func()
            raise
        obj._vals_.update((attr.name, new_val) for attr, new_val in avdict.iteritems())
    def _keyargs_to_avdicts_(obj, keyargs):
        avdict, collection_avdict = {}, {}
        get = obj._adict_.get
        for name, new_val in keyargs.items():
            attr = get(name)
            if attr is None: raise TypeError('Unknown attribute %r' % name)
            new_val = attr.check(new_val, obj, from_db=False)
            if not attr.is_collection:
                if attr.pk_offset is not None:
                    old_val = obj._vals_.get(attr.name, NOT_LOADED)
                    if old_val != new_val: raise TypeError('Cannot change value of primary key attribute %s' % attr.name)
                else: avdict[attr] = new_val
            else: collection_avdict[attr] = new_val
        return avdict, collection_avdict
    @cut_traceback
    def check_on_commit(obj):
        cache = obj._cache_
        if not cache.is_alive: raise TransactionRolledBack('Object belongs to obsolete cache')
        if obj._status_ not in ('loaded', 'saved'): return
        obj._status_ = 'locked'
        cache.to_be_checked.append(obj)
    @classmethod
    def _attrs_with_bit_(entity, mask=-1):
        get_bit = entity._bits_.get
        for attr in entity._attrs_:
            bit = get_bit(attr)
            if bit is None: continue
            if not bit & mask: continue
            yield attr
    def _save_principal_objects_(obj, dependent_objects):
        if dependent_objects is None: dependent_objects = []
        elif obj in dependent_objects:
            chain = ' -> '.join(obj2.__class__.__name__ for obj2 in dependent_objects)
            raise UnresolvableCyclicDependency('Cannot save cyclic chain: ' + chain)
        dependent_objects.append(obj)
        status = obj._status_
        if status == 'created': attr_iter = obj._attrs_with_bit_()
        elif status == 'updated': attr_iter = obj._attrs_with_bit_(obj._wbits_)
        else: assert False
        for attr in attr_iter:
            val = obj._vals_[attr.name]
            if not attr.reverse: continue
            if not attr.columns: continue
            if val is None: continue
            if val._status_ == 'created':
                val._save_(dependent_objects)
                assert val._status_ == 'saved'
    def _save_created_(obj):
        values = []
        auto_pk = (obj._pkval_ is None)
        if auto_pk: pk_attr = obj.__class__._pk_
        for attr in obj._attrs_:
            if not attr.columns: continue
            if attr.is_collection: continue
            val = obj._vals_[attr.name]
            if auto_pk and attr.is_pk: continue
            values.extend(attr.get_raw_values(val))
        database = obj._database_
        if auto_pk: cached_sql = obj._cached_create_sql_auto_pk_
        else: cached_sql = obj._cached_create_sql_
        if cached_sql is None:
            entity = obj.__class__
            if auto_pk:
                columns = entity._columns_without_pk_
                converters = entity._converters_without_pk_
            else:
                columns = entity._columns_
                converters = entity._converters_
            assert len(columns) == len(converters)
            params = [ [ PARAM, i,  converter ] for i, converter in enumerate(converters) ]
            sql_ast = [ INSERT, entity._table_, columns, params ]
            if auto_pk:
                assert len(entity._pk_columns_) == 1
                assert pk_attr.auto
                sql_ast.append(obj._pk_columns_[0])
            sql, adapter = database._ast2sql(sql_ast)
            if auto_pk: entity._cached_create_sql_auto_pk_ = sql, adapter
            else: entity._cached_create_sql_ = sql, adapter
        else: sql, adapter = cached_sql
        arguments = adapter(values)
        try:
            if auto_pk: new_id = database._exec_sql_returning_id(sql, arguments, pk_attr.py_type)
            else: database._exec_sql(sql, arguments)
        except IntegrityError, e:
            msg = " ".join(tostring(arg) for arg in e.args)
            raise TransactionIntegrityError(
                'Object %r cannot be stored in the database (probably it already exists). DB message: %s' % (obj, msg), e)
        except DatabaseError, e:
            msg = " ".join(tostring(arg) for arg in e.args)
            raise UnexpectedError('Object %r cannot be stored in the database. DB message: %s' % (obj, msg), e)

        if auto_pk:
            index = obj._cache_.indexes.setdefault(pk_attr, {})
            obj2 = index.setdefault(new_id, obj)
            if obj2 is not obj: raise TransactionIntegrityError(
                'Newly auto-generated id value %s was already used in transaction cache for another object' % new_id)
            obj._pkval_ = obj._vals_[pk_attr.name] = new_id
            obj._newid_ = None
            
        obj._status_ = 'saved'
        obj._rbits_ = obj._all_bits_
        obj._wbits_ = 0
        bits = obj._bits_
        for attr in obj._attrs_:
            if attr not in bits: continue
            obj._dbvals_[attr.name] = obj._vals_[attr.name]
    def _save_updated_(obj):
        update_columns = []
        values = []
        for attr in obj._attrs_with_bit_(obj._wbits_):
            if not attr.columns: continue
            update_columns.extend(attr.columns)
            val = obj._vals_[attr.name]
            values.extend(attr.get_raw_values(val))
        if update_columns:
            for attr in obj._pk_attrs_:
                val = obj._vals_[attr.name]
                values.extend(attr.get_raw_values(val))
            optimistic_check_columns = []
            optimistic_check_converters = []
            if obj._cache_.optimistic:
                for attr in obj._attrs_with_bit_(obj._rbits_):
                    if not attr.columns: continue
                    dbval = obj._dbvals_.get(attr.name, NOT_LOADED)
                    assert dbval is not NOT_LOADED
                    optimistic_check_columns.extend(attr.columns)
                    if dbval is not None:
                        optimistic_check_converters.extend(attr.converters)
                    else:
                        optimistic_check_converters.extend(None for converter in attr.converters)
                    values.extend(attr.get_raw_values(dbval))
            query_key = (tuple(update_columns), tuple(optimistic_check_columns), tuple(converter is not None for converter in optimistic_check_converters))
            database = obj._database_
            cached_sql = obj._update_sql_cache_.get(query_key)
            if cached_sql is None:
                update_converters = []
                for attr in obj._attrs_with_bit_(obj._wbits_):
                    if not attr.columns: continue
                    update_converters.extend(attr.converters)
                assert len(update_columns) == len(update_converters)
                update_params = [ [ PARAM, i, converter ] for i, converter in enumerate(update_converters) ]
                params_count = len(update_params)
                where_list = [ WHERE ]
                pk_columns = obj._pk_columns_
                pk_converters = obj._pk_converters_
                params_count = populate_criteria_list(where_list, pk_columns, pk_converters, params_count)
                populate_criteria_list(where_list, optimistic_check_columns, optimistic_check_converters, params_count)
                sql_ast = [ UPDATE, obj._table_, zip(update_columns, update_params), where_list ]
                sql, adapter = database._ast2sql(sql_ast)
                obj._update_sql_cache_[query_key] = sql, adapter
            else: sql, adapter = cached_sql
            arguments = adapter(values)
            cursor = database._exec_sql(sql, arguments)
            if cursor.rowcount != 1:
                raise UnrepeatableReadError('Object %r was updated outside of current transaction' % obj)
        obj._status_ = 'saved'
        obj._rbits_ |= obj._wbits_
        obj._wbits_ = 0
        for attr in obj._attrs_with_bit_():
            val = obj._vals_.get(attr.name, NOT_LOADED)
            if val is NOT_LOADED: assert attr.name not in obj._dbvals_
            else: obj._dbvals_[attr.name] = val
    def _save_locked_(obj):
        assert obj._wbits_ == 0
        if not obj._cache_.optimistic:
            obj._status_ = 'loaded'
            return
        values = []
        for attr in obj._pk_attrs_:
            val = obj._vals_[attr.name]
            values.extend(attr.get_raw_values(val))
        optimistic_check_columns = []
        optimistic_check_converters = []
        for attr in obj._attrs_with_bit_(obj._rbits_):
            if not attr.columns: continue
            dbval = obj._dbvals_.get(attr.name, NOT_LOADED)
            assert dbval is not NOT_LOADED
            optimistic_check_columns.extend(attr.columns)
            optimistic_check_converters.extend(attr.converters)
            values.extend(attr.get_raw_values(dbval))
        query_key = tuple(optimistic_check_columns)
        database = obj._database_
        cached_sql = obj._lock_sql_cache_.get(query_key)        
        if cached_sql is None:
            where_list = [ WHERE ]
            params_count = populate_criteria_list(where_list, obj._pk_columns_, obj._pk_converters_)
            populate_criteria_list(where_list, optimistic_check_columns, optimistic_check_converters, params_count)
            sql_ast = [ SELECT, [ ALL, [ VALUE, 1 ]], [ FROM, [ None, TABLE, obj._table_ ] ], where_list ]
            sql, adapter = database._ast2sql(sql_ast)
            obj._lock_sql_cache_[query_key] = sql, adapter
        else: sql, adapter = cached_sql
        arguments = adapter(values)
        cursor = database._exec_sql(sql, arguments)
        row = cursor.fetchone()
        if row is None: raise UnrepeatableReadError('Object %r was updated outside of current transaction' % obj)
        obj._status_ = 'loaded'
    def _save_deleted_(obj):
        database = obj._database_
        cached_sql = obj._cached_delete_sql_
        if cached_sql is None:
            where_list = [ WHERE ]
            populate_criteria_list(where_list, obj._pk_columns_, obj._pk_converters_)
            sql_ast = [ DELETE, obj._table_, where_list ]
            sql, adapter = database._ast2sql(sql_ast)
            obj.__class__._cached_delete_sql_ = sql, adapter
        else: sql, adapter = cached_sql
        values = obj._get_raw_pkval_()
        arguments = adapter(values)
        database._exec_sql(sql, arguments)
    def _save_(obj, dependent_objects=None):
        assert obj._cache_.is_alive
        status = obj._status_
        if status in ('loaded', 'saved', 'cancelled'): return
        if status in ('created', 'updated'):
            obj._save_principal_objects_(dependent_objects)

        if status == 'created': obj._save_created_()
        elif status == 'updated': obj._save_updated_()
        elif status == 'deleted': obj._save_deleted_()
        elif status == 'locked': obj._save_locked_()
        else: assert False

class Cache(object):
    def __init__(cache, database, connection):
        cache.is_alive = True
        cache.database = database
        cache.connection = connection
        cache.num = next_num()
        cache.optimistic = database.optimistic
        cache.ignore_none = True  # todo : get from provider
        cache.indexes = {}
        cache.seeds = {}
        cache.collection_statistics = {}
        cache.created = set()
        cache.deleted = set()
        cache.updated = set()
        cache.modified_collections = {}
        cache.to_be_checked = []
    def flush(cache):
        assert cache.is_alive
        cache.save(False)
    def commit(cache):
        assert cache.is_alive
        database = cache.database
        provider = database.provider
        connection = cache.connection
        try:
            if cache.optimistic:
                if debug: print 'OPTIMISTIC ROLLBACK\n'
                provider.rollback(connection)
        except:
            cache.is_alive = False
            cache.connection = None
            x = local.db2cache.pop(database); assert x is cache
            provider.drop(connection)
            raise
        save_is_needed = cache.has_anything_to_save()
        try:
            if save_is_needed: cache.save()
            if save_is_needed or not cache.optimistic:
                if debug: print 'COMMIT\n'
                provider.commit(connection)
        except:
            cache.rollback()
            raise
    def rollback(cache, close_connection=False):
        assert cache.is_alive
        database = cache.database
        x = local.db2cache.pop(database); assert x is cache
        cache.is_alive = False
        provider = database.provider
        connection = cache.connection
        cache.connection = None
        try:
            if debug: print 'ROLLBACK\n'
            provider.rollback(connection)
            if not close_connection:
                if debug: print 'RELEASE_CONNECTION\n'
                provider.release(connection)
        except:
            if debug: print 'CLOSE_CONNECTION\n'
            provider.drop(connection)
            raise
        if close_connection:
            if debug: print 'CLOSE_CONNECTION\n'
            provider.drop(connection)
    def release(cache):
        assert cache.is_alive
        database = cache.database
        x = local.db2cache.pop(database); assert x is cache
        cache.is_alive = False
        provider = database.provider
        connection = cache.connection
        cache.connection = None
        if debug: print 'RELEASE_CONNECTION\n'
        provider.release(connection)
    def has_anything_to_save(cache):
        return bool(cache.created or cache.updated or cache.deleted or cache.modified_collections)                    
    def save(cache, optimistic=True):
        cache.optimistic = optimistic
        if not cache.has_anything_to_save(): return
        modified_m2m = cache.calc_modified_m2m()
        for attr, (added, removed) in modified_m2m.iteritems():
            if not removed: continue
            attr.remove_m2m(removed)
        for obj in cache.to_be_checked:
            obj._save_()
        for attr, (added, removed) in modified_m2m.iteritems():
            if not added: continue
            attr.add_m2m(added)

        cache.created.clear()
        cache.updated.clear()

        indexes = cache.indexes
        for obj in cache.deleted:
            pkval = obj._pkval_
            index = indexes[obj.__class__._pk_]
            index.pop(pkval)
            
        cache.deleted.clear()
        cache.modified_collections.clear()
        cache.to_be_checked[:] = []
    def calc_modified_m2m(cache):
        modified_m2m = {}
        for attr, objects in cache.modified_collections.iteritems():
            if not isinstance(attr, Set): raise NotImplementedError
            reverse = attr.reverse
            if not reverse.is_collection: continue
            if not isinstance(reverse, Set): raise NotImplementedError
            if reverse in modified_m2m: continue
            added, removed = modified_m2m.setdefault(attr, (set(), set()))
            for obj in objects:
                setdata = obj._vals_[attr.name]
                for obj2 in setdata.added: added.add((obj, obj2))
                for obj2 in setdata.removed: removed.add((obj, obj2))
        return modified_m2m
    def update_simple_index(cache, obj, attr, old_val, new_val, undo):
        index = cache.indexes.get(attr)
        if index is None: index = cache.indexes[attr] = {}
        if new_val is None and cache.ignore_none: new_val = NO_UNDO_NEEDED
        else:
            obj2 = index.setdefault(new_val, obj)
            if obj2 is not obj: raise CacheIndexError('Cannot update %s.%s: %s with key %s already exists'
                                                 % (obj.__class__.__name__, attr.name, obj2, new_val))
        if old_val is NOT_LOADED: old_val = NO_UNDO_NEEDED
        elif old_val is None and cache.ignore_none: old_val = NO_UNDO_NEEDED
        else: del index[old_val]
        undo.append((index, old_val, new_val))
    def db_update_simple_index(cache, obj, attr, old_dbval, new_dbval):
        index = cache.indexes.get(attr)
        if index is None: index = cache.indexes[attr] = {}
        if new_dbval is NOT_LOADED: pass
        elif new_dbval is None and cache.ignore_none: pass
        else:
            obj2 = index.setdefault(new_dbval, obj)
            if obj2 is not obj: raise TransactionIntegrityError(
                '%s with unique index %s.%s already exists: %s'
                % (obj2.__class__.__name__, obj.__class__.__name__, attr.name, new_keyval))
                # attribute which was created or updated lately clashes with one stored in database
        index.pop(old_dbval, None)
    def update_composite_index(cache, obj, attrs, currents, vals, undo):
        if cache.ignore_none:
            if None in currents: currents = NO_UNDO_NEEDED
            if None in vals: vals = NO_UNDO_NEEDED
        if currents is NO_UNDO_NEEDED: pass
        elif NOT_LOADED in currents: currents = NO_UNDO_NEEDED
        if vals is NO_UNDO_NEEDED: pass
        elif NOT_LOADED in vals: vals = NO_UNDO_NEEDED
        if currents is NO_UNDO_NEEDED and vals is NO_UNDO_NEEDED: return
        index = cache.indexes.get(attrs)
        if index is None: index = cache.indexes[attrs] = {}
        if vals is NO_UNDO_NEEDED: pass
        else:
            obj2 = index.setdefault(vals, obj)
            if obj2 is not obj:
                attr_names = ', '.join(attr.name for attr in attrs)
                raise CacheIndexError('Cannot update %r: composite key (%s) with value %s already exists for %r'
                                 % (obj, attr_names, vals, obj2))
        if currents is NO_UNDO_NEEDED: pass
        else: del index[currents]
        undo.append((index, currents, vals))
    def db_update_composite_index(cache, obj, attrs, currents, vals):
        index = cache.indexes.get(attrs)
        if index is None: index = cache.indexes[attrs] = {}
        if NOT_LOADED in vals: pass
        elif None in vals and cache.ignore_none: pass
        else:
            obj2 = index.setdefault(vals, obj)
            if obj2 is not obj:
                key_str = ', '.join(repr(item) for item in new_keyval)
                raise TransactionIntegrityError('%s with unique index %s.%s already exists: %s'
                                                % (obj2.__class__.__name__, obj.__class__.__name__, attr.name, key_str))
        index.pop(currents, None)

def _get_caches():
    return list(sorted((cache for cache in local.db2cache.values()),
                       reverse=True, key=lambda cache : (cache.database.priority, cache.num)))

@cut_traceback
def flush():
    for cache in _get_caches(): cache.flush()
        
@cut_traceback
def commit():
    caches = _get_caches()
    if not caches: return
    primary_cache = caches[0]
    other_caches = caches[1:]
    exceptions = []
    try:
        try: primary_cache.commit()
        except:
            exceptions.append(sys.exc_info())
            for cache in other_caches:
                try: cache.rollback()
                except: exceptions.append(sys.exc_info())
            reraise(CommitException, exceptions)
        for cache in other_caches:
            try: cache.commit()
            except: exceptions.append(sys.exc_info())
        if exceptions:
            reraise(PartialCommitException, exceptions)
    finally:
        del exceptions
        
@cut_traceback
def rollback():
    exceptions = []
    try:
        for cache in _get_caches():
            try: cache.rollback()
            except: exceptions.append(sys.exc_info())
        if exceptions:
            reraise(RollbackException, exceptions)
        assert not local.db2cache
    finally:
        del exceptions

def _release():
    for cache in _get_caches(): cache.release()
    assert not local.db2cache

def _with_transaction(func, args, keyargs, allowed_exceptions=[]):
    try:
        try: result = func(*args, **keyargs)
        except Exception, e:
            exc_info = sys.exc_info()
            try:
                # write to log
                for exc_class in allowed_exceptions:
                    if isinstance(e, exc_class):
                        commit()
                        break
                else: rollback()
            finally:
                try:
                    raise exc_info[0], exc_info[1], exc_info[2]
                finally: del exc_info
        commit()
        return result
    finally: _release()

@decorator_with_params
def with_transaction(func, retry=1, retry_exceptions=[ TransactionError ], allowed_exceptions=[]):
    @cut_traceback
    def new_func(*args, **keyargs):
        counter = retry
        while counter > 0:
            try: return _with_transaction(func, args, keyargs, allowed_exceptions)
            except Exception, e:
                for exc_class in retry_exceptions:
                    if isinstance(e, exc_class): break # for
                else: raise                    
            counter -= 1
    return new_func

@simple_decorator
def db_decorator(func, *args, **keyargs):
    web = sys.modules.get('pony.web')
    allowed_exceptions = web and [ web.HttpRedirect ] or []
    try: return _with_transaction(func, args, keyargs, allowed_exceptions)
    except (ObjectNotFound, RowNotFound):
        if web: raise web.Http404NotFound
        raise

###############################################################################

python_ast_cache = {}
sql_cache = {}

@cut_traceback
def query(gen, frame_depth=0):
    if isinstance(gen, types.GeneratorType):
        tree, external_names = decompile(gen)
        code = gen.gi_frame.f_code
        globals = gen.gi_frame.f_globals
        locals = gen.gi_frame.f_locals
    elif isinstance(gen, basestring):
        gen_src = '(%s)' % gen
        module_node = parse(gen_src)
        if not isinstance(module_node, ast.Module): raise TypeError
        stmt_node = module_node.node
        if not isinstance(stmt_node, ast.Stmt) or len(stmt_node.nodes) != 1: raise TypeError
        discard_node = stmt_node.nodes[0]
        if not isinstance(discard_node, ast.Discard): raise TypeError
        tree = discard_node.expr
        if not isinstance(tree, ast.GenExpr): raise TypeError
        code = gen_src
        internal_names = set()
        external_names = set()
        def collect_names(tree, internals, externals):
            for child in tree.getChildNodes():
                if isinstance(child, ast.Name): externals.add(child.name)
                elif isinstance(child, ast.AssName): internals.add(child.name)
                elif isinstance(child, ast.GenExpr):
                    inner_internals = set()
                    inner_externals = set()
                    collect_names(child, inner_internals, inner_externals)
                    inner_externals -= inner_internals
                    for name in inner_externals:
                        if name not in internals: externals.add(name)
                else: collect_names(child, internals, externals)
        collect_names(tree, internal_names, external_names)
        external_names -= internal_names
        globals = sys._getframe(frame_depth+2).f_globals
        locals = sys._getframe(frame_depth+2).f_locals
    else: raise TypeError
    return Query(code, tree.code, external_names, globals, locals)

@cut_traceback
def fetch(gen):
    return query(gen, frame_depth=2).fetch()

@cut_traceback
def fetch_one(gen):
    return query(gen, frame_depth=2).fetch_one()

@cut_traceback
def fetch_count(gen):
    return query(gen).count()

@cut_traceback
def fetch_sum(gen):
    return query(gen).sum()

@cut_traceback
def fetch_min(gen):
    return query(gen).min()

@cut_traceback
def fetch_max(gen):
    return query(gen).max()

@cut_traceback
def fetch_avg(gen):
    return query(gen).avg()

@cut_traceback
def exists(gen):
    return select(gen).exists()

def JOIN(expr):
    return expr

class AsciiStr(str): pass

class Query(object):
    def __init__(query, code, tree, external_names, globals, locals):
        assert isinstance(tree, ast.GenExprInner)
        query._tree = tree
        query._external_names = external_names

        query._databases = databases = {}
        query._entities = entities = {}
        query._variables = variables = {}
        query._vartypes = vartypes = {}
        query._functions = functions = {}

        node = tree.quals[0].iter
        assert isinstance(node, ast.Name)
        name = node.name
        try: origin = locals[name]
        except KeyError: assert False

        if isinstance(origin, EntityIter): origin = origin.entity
        elif not isinstance(origin, EntityMeta):
            raise NotImplementedError, 'Query iterator has unexpected type %r' % type(origin).__name__
        database = origin._database_
        if database is None: raise TranslationError('Entity %s is not mapped to a database' % origin.__name__)
        
        provider = database.provider
        translator_cls = database.provider.translator_cls

        for name in external_names:
            try: value = locals[name]
            except KeyError:
                try: value = globals[name]
                except KeyError:
                    try: value = getattr(__builtin__, name)
                    except AttributeError: raise NameError, name
            try: hash(value)
            except TypeError: raise TypeError, 'Variable %r has unexpected type %r' % (name, type(value).__name__)
            if value in translator_cls.special_functions: functions[name] = value
            elif type(value) in (types.FunctionType, types.BuiltinFunctionType):
                raise TypeError('Function %r cannot be used inside query' % value.__name__)
            elif type(value) is types.MethodType:
                raise TypeError('Method %r cannot be used inside query' % value.__name__)
            elif isinstance(value, EntityMeta):
                entities[name] = value
            elif isinstance(value, EntityIter):
                entities[name] = value.entity
            elif isinstance(value, Database):
                databases[name] = value
            else:
                variables[name] = value
                try: normalized_type = translator_cls.get_normalized_type_of(value)
                except TypeError: raise TypeError("Variable %r has unexpected type %r" % (name, type(value).__name__))
                vartypes[name] = normalized_type

        query._result = None
        key = id(code), tuple(sorted(databases.iteritems())), tuple(sorted(entities.iteritems())), \
                        tuple(sorted(vartypes.iteritems())), tuple(sorted(functions.iteritems()))
        query._python_ast_key = key

        query._database = database
        translator = python_ast_cache.get(key)
        if translator is None:
            translator = translator_cls(tree, databases, entities, vartypes, functions)
            python_ast_cache[key] = translator
        query._translator = translator
        query._order = tuple((attr, True) for attr in translator.entity._pk_attrs_)
    def _construct_sql(query, order=None, range=None, aggr_func_name=None):
        translator = query._translator
        sql_key = query._python_ast_key + (order, range, aggr_func_name, options.INNER_JOIN_SYNTAX)
        cache_entry = sql_cache.get(sql_key)
        database = query._database
        if cache_entry is None:
            sql_ast = [ SELECT ]
            if aggr_func_name:
                attr = translator.attr
                if attr is not None:
                    attr_type = translator.normalize_type(attr.py_type)
                    if aggr_func_name in (SUM, AVG) and attr_type not in translator.numeric_types:
                        raise TranslationError('%r is valid for numeric attributes only' % aggr_func_name.lower())
                    column_ast = [ COLUMN, translator.alias, attr.column ]
                elif aggr_func_name is not COUNT: raise TranslationError(
                    'Attribute should be specified for %r aggregate function' % aggr_func_name.lower())
                if aggr_func_name is COUNT:
                    if attr is None: aggr_ast = [ COUNT, ALL ]
                    else: aggr_ast = [ COUNT, DISTINCT, column_ast ]
                elif aggr_func_name is SUM: aggr_ast = [ COALESCE, [ SUM, column_ast ], [ VALUE, 0 ] ]
                else: aggr_ast = [ aggr_func_name, column_ast ]
                sql_ast.append([ AGGREGATES, aggr_ast ])
            else: sql_ast.append(translator.select)
            sql_ast.append(translator.from_)
            sql_ast.append(translator.where)
            if order:
                alias = translator.alias
                orderby_section = [ ORDER_BY ]
                for attr, asc in order:
                    for column in attr.columns:
                        orderby_section.append(([COLUMN, alias, column], asc and ASC or DESC))
                sql_ast = sql_ast + [ orderby_section ]
            if range:
                start, stop = range
                limit = stop - start
                offset = start
                assert limit is not None
                limit_section = [ LIMIT, [ VALUE, limit ]]
                if offset: limit_section.append([ VALUE, offset ])
                sql_ast = sql_ast + [ limit_section ]
            cache = database._get_cache()
            sql, adapter = database.provider.ast2sql(sql_ast)
            cache_entry = sql, adapter
            sql_cache[sql_key] = cache_entry
        else: sql, adapter = cache_entry
        return sql, adapter
    def _exec_sql(query, order=None, range=None, aggr_func_name=None):
        sql, adapter = query._construct_sql(order, range, aggr_func_name)
        param_dict = {}
        for param_name, extractor in query._translator.extractors.items():
            param_dict[param_name] = extractor(query._variables)
        arguments = adapter(param_dict)
        cursor = query._database._exec_sql(sql, arguments)
        return cursor
    def _fetch(query, range=None):
        translator = query._translator
        cursor = query._exec_sql(query._order, range)
        result = translator.entity._fetch_objects(cursor, translator.attr_offsets)
        if translator.attr is None: return result
        return map(attrgetter(translator.attr.name), result)
    @cut_traceback
    def fetch(query):
        return query._fetch()
    @cut_traceback
    def fetch_one(query):
        objects = query[:2]
        if not objects: return None
        if len(objects) > 1: raise MultipleObjectsFoundError(
            'Multiple objects was found. Use select(..).fetch() to retrieve them')
        return objects[0]
    @cut_traceback
    def exists(query):
        new_query = query._clone()
        new_query._aggr_func_name = EXISTS
        new_query._aggr_select = [ ALL, [ VALUE, 1 ] ]
        cursor = new_query._exec_sql(range=(0, 1))
        row = cursor.fetchone()
        return row is not None
    @cut_traceback
    def __iter__(query):
        return iter(query.fetch())
    @cut_traceback
    def orderby(query, *args):
        if not args: raise TypeError('query.orderby() requires at least one argument')
        entity = query._translator.entity
        order = []
        if args == (None,): pass
        else:
            for arg in args:
                if isinstance(arg, Attribute): order.append((arg, True))
                elif isinstance(arg, DescWrapper): order.append((arg.attr, False))
                else: raise TypeError('query.orderby() arguments must be attributes. Got: %r' % arg)
                attr = order[-1][0]
                if entity._adict_.get(attr.name) is not attr: raise TypeError(
                    'Attribute %s does not belong to Entity %s' % (attr, entity.__name__))
        new_query = query._clone()
        new_query._order = tuple(order)
        return new_query
    def _clone(query):
        new_query = object.__new__(Query)
        new_query.__dict__.update(query.__dict__)
        return new_query
    @cut_traceback
    def __getitem__(query, key):
        if isinstance(key, slice):
            step = key.step
            if step is not None and step <> 1: raise TypeError("Parameter 'step' of slice object is not allowed here")
            start = key.start
            if start is None: start = 0
            elif start < 0: raise TypeError("Parameter 'start' of slice object cannot be negative")
            stop = key.stop
            if stop is None:
                if not start: return query.fetch()
                else: raise TypeError("Parameter 'stop' of slice object should be specified")
        else:
            try: i = key.__index__()
            except AttributeError:
                try: i = key.__int__()
                except AttributeError: raise TypeError('Incorrect argument type: %r' % key)
            result = query._fetch((i, i+1))
            return result[0]
        if start >= stop: return []
        return query._fetch((start, stop))
    @cut_traceback
    def limit(query, limit, offset=None):
        start = offset or 0
        stop = start + limit
        return query[start:stop]
    def _aggregate(query, aggr_func_name):
        translator = query._translator
        cursor = query._exec_sql(None, None, aggr_func_name)
        row = cursor.fetchone()
        if row is not None: result = row[0]
        else: result = None
        if result is None:
            if aggr_func_name in (SUM, COUNT): result = 0
            else: return None
        if aggr_func_name is COUNT: return result
        converter = translator.attr.converters[0]
        return converter.sql2py(result)
    @cut_traceback
    def sum(query):
        return query._aggregate(SUM)
    @cut_traceback
    def avg(query):
        return query._aggregate(AVG)
    @cut_traceback
    def min(query):
        return query._aggregate(MIN)
    @cut_traceback
    def max(query):
        return query._aggregate(MAX)
    @cut_traceback
    def count(query):
        return query._aggregate(COUNT)

def show(entity):
    x = entity
    if isinstance(x, EntityMeta):
        print x.describe()
    elif isinstance(x, Entity):
        print 'instance of ' + x.__class__.describe()
    else:
        from pprint import pprint
        pprint(x)
