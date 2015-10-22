# -*- encoding: utf-8 -*-

import json
import re
import logging
from collections import namedtuple
try:
    from functools import reduce
except ImportError:
    pass

from django.db.models import Model, Manager
from django.views.generic.list import ListView, MultipleObjectMixin
from django.http import HttpResponse
from django.forms.util import flatatt
from django.template.loader import render_to_string
from django.conf import settings
from django.utils.encoding import force_text
try:
    from django.utils.encoding import python_2_unicode_compatible
except ImportError:
    from .compat import python_2_unicode_compatible

import six

from .base import DatatableMixin
from ..datatables import Datatable, LegacyDatatable
from ..utils import (apply_options, get_datatable_structure, DatatableOptions, OPTION_NAME_MAP,
                     DEFAULT_PAGE_LENGTH, MINIMUM_PAGE_LENGTH)

log = logging.getLogger(__name__)


_javascript_boolean = {
    True: 'true',
    False: 'false',
}

FieldDefinitionTuple = namedtuple('FieldDefinitionTuple', ['pretty_name', 'fields', 'callback'])
ColumnOrderingTuple = namedtuple('ColumnOrderingTuple', ['order', 'column_index', 'direction'])
ColumnInfoTuple = namedtuple('ColumnInfoTuple', ['pretty_name', 'attrs'])

DEFAULT_OPTIONS = {
    'columns': [],  # table headers
    'ordering': [],  # override to Model._meta.ordering
    'start_offset': 0,  # results to skip ahead
    'page_length': 25,  # length of a single result page
    'search': '',  # client search string
    'search_fields': [],  # extra ORM paths to search; not displayed
    'unsortable_columns': [],  # table headers not allowed to be sorted
    'hidden_columns': [],  # table headers to be generated, but hidden by the client
    'structure_template': "datatableview/legacy_structure.html",
    'result_counter_id': 'id_count',  # HTML element ID to display the total results
}

def get_field_definition(field_definition):
    """ Normalizes a field definition into its component parts, even if some are missing. """
    if not isinstance(field_definition, (tuple, list)):
        field_definition = [field_definition]
    else:
        field_definition = list(field_definition)

    if len(field_definition) == 1:
        field = [None, field_definition, None]
    elif len(field_definition) == 2:
        field = field_definition + [None]
    elif len(field_definition) == 3:
        field = field_definition
    else:
        raise ValueError("Invalid field definition format.")

    if not isinstance(field[1], (tuple, list)):
        field[1] = (field[1],)
    field[1] = tuple(name for name in field[1] if name is not None)

    return FieldDefinitionTuple(*field)


class ObjectListResult(list):
    _dtv_total_initial_record_count = None
    _dtv_unpaged_total = None


@python_2_unicode_compatible
class DatatableStructure(object):
    """
    A class designed to be echoed directly to into template HTML to represent a skeleton table
    structure that datatables.js can use.

    """

    def __init__(self, ajax_url, options, model=None):
        self.url = ajax_url
        self.options = options
        self.model = model

        self.ordering = {}
        if options['ordering']:
            for i, name in enumerate(options['ordering']):
                plain_name = name.lstrip('-+')
                index = options.get_column_index(plain_name)
                if index == -1:
                    continue
                sort_direction = 'desc' if name[0] == '-' else 'asc'
                self.ordering[plain_name] = ColumnOrderingTuple(i, index, sort_direction)

    def __str__(self):
        context = {
            'url': self.url,
            'column_info': self.get_column_info(),
        }
        context.update(self.options)
        return render_to_string(self.options['structure_template'], context)

    def __iter__(self):
        """
        Yields the column information suitable for rendering HTML.

        Each time is returned as a 2-tuple in the form ("Column Name", "data-attribute='asdf'"),

        """

        for column_info in self.get_column_info():
            yield column_info

    def get_column_info(self):
        """
        Returns an iterable of 2-tuples in the form

            ("Pretty name", ' data-bSortable="true"',)

        """

        column_info = []
        if self.model:
            # get_all_field_names is deprecated in Django 1.8, this also fixes proxied models
            if hasattr(self.model._meta, 'get_fields'):
                model_fields = [field.name for field in self.model._meta.get_fields()]
            else:
                model_fields = self.model._meta.get_all_field_names()
        else:
            model_fields = []

        for column in self.options['columns']:
            column = get_field_definition(column)
            pretty_name = column.pretty_name
            column_name = column.pretty_name
            if column.fields and column.fields[0] in model_fields:
                ordering_name = column.fields[0]
                if not pretty_name:
                    field = self.model._meta.get_field_by_name(column.fields[0])[0]
                    column_name = field.name
                    pretty_name = field.verbose_name
            else:
                ordering_name = pretty_name

            attributes = self.get_column_attributes(ordering_name)
            column_info.append(ColumnInfoTuple(pretty_name, flatatt(attributes)))

        return column_info

    def get_column_attributes(self, name):
        attributes = {
            'data-sortable': _javascript_boolean[name not in self.options['unsortable_columns']],
            'data-visible': _javascript_boolean[name not in self.options['hidden_columns']],
        }

        if name in self.ordering:
            attributes['data-sorting'] = ','.join(map(six.text_type, self.ordering[name]))

        return attributes


class LegacyDatatableMixin(MultipleObjectMixin):
    """
    Converts a view into an AJAX interface for obtaining records.

    The normal GET execution adds a ``DataTable`` object to the context which can be used to
    streamline the dumping of the HTML skeleton required for datatables.js to hook.  A ``DataTable``
    object doesn't hold any data, just a structure superficially generated from the options on the
    view.

    The template is responsible for making the AJAX request back to this view to populate the table
    with data.

    """

    datatable_options = None
    datatable_context_name = 'datatable'

    def get(self, request, *args, **kwargs):
        """
        Detects AJAX access and returns appropriate serialized data.  Normal access to the view is
        unmodified.
        """

        if request.is_ajax() or request.GET.get('ajax') == 'true':
            return self.get_ajax(request, *args, **kwargs)
        return super(LegacyDatatableMixin, self).get(request, *args, **kwargs)

    def get_ajax(self, request, *args, **kwargs):
        """ Called in place of normal ``get()`` when accessed via AJAX. """

        object_list = self.get_object_list()
        total = object_list._dtv_total_initial_record_count
        filtered_total = object_list._dtv_unpaged_total
        response_data = self.get_json_response_object(object_list, total, filtered_total)
        response = HttpResponse(self.serialize_to_json(response_data),
                                content_type="application/json")

        return response

    def get_object_list(self):
        """ Gets the core queryset, but applies the datatable options to it. """
        object_list = self.apply_queryset_options(self.get_queryset())

        # Stats get redirected to the underlying Datatable object in the current code
        object_list = ObjectListResult(object_list)
        object_list._dtv_total_initial_record_count = self._modern_datatable.total_initial_record_count
        object_list._dtv_unpaged_total = self._modern_datatable.unpaged_record_count
        return object_list

    def get_datatable_options(self):
        """
        Returns the DatatableOptions object for this view's configuration.

        This method is guaranteed to be called only once per request.

        """

        return self.datatable_options

    def _get_datatable_options(self):
        """
        Internal safe access.  Guarantees that ``get_datatable_options()`` is called only once, so
        that subclasses can use that method to modify the class attribute ``datatable_options``.

        """

        if not hasattr(self, '_datatable_options'):
            if self.model is None:
                self.model = self.get_queryset().model

            options = self.get_datatable_options()
            if options:
                # Options are defined, but probably in a raw dict format
                options = DatatableOptions(self.model, self.request.GET, **dict(options))
            else:
                # No options defined on the view
                options = DatatableOptions(self.model, self.request.GET)

            self._datatable_options = options
        return self._datatable_options

    def apply_queryset_options(self, queryset):
        """
        Interprets the datatable options.

        Options requiring manual massaging of the queryset are handled here.  The output of this
        method should be treated as a list, since complex options might convert it out of the
        original queryset form.

        """

        kwargs = {
            'object_list': queryset,
            'view': self,
            'model': self.model,
            'url': self.request.path,
            'query_config': self.request.GET,
            'callback_target': self,
        }
        kwargs.update(self._get_datatable_options())
        self._modern_datatable = LegacyDatatable(**kwargs)

        return apply_options(self._modern_datatable.object_list, self._modern_datatable)

    def get_datatable_context_name(self):
        return self.datatable_context_name

    def get_datatable(self):
        """
        Returns the helper object that can be used in the template to render the datatable skeleton.
        """

        options = self._get_datatable_options()
        return get_datatable_structure(self.request.path, options, model=self.model)

    def get_context_data(self, **kwargs):
        context = super(LegacyDatatableMixin, self).get_context_data(**kwargs)
        context[self.get_datatable_context_name()] = self.get_datatable()
        return context

    def get_json_response_object(self, object_list, total, filtered_total):
        """
        Returns the JSON-compatible dictionary that will be serialized for an AJAX response.

        The value names are in the form "s~" for strings, "i~" for integers, and "a~" for arrays,
        if you're unfamiliar with the old C-style jargon used in dataTables.js.  "aa~" means
        "array of arrays".  In some instances, the author uses "ao~" for "array of objects", an
        object being a javascript dictionary.
        """

        object_list_page = self.paginate_object_list(object_list)

        response_obj = {
            'sEcho': self.request.GET.get('sEcho', None),
            'iTotalRecords': total,
            'iTotalDisplayRecords': filtered_total,
            'aaData': [self.get_record_data(obj) for obj in object_list_page],
        }
        return response_obj

    def paginate_object_list(self, object_list):
        """
        If page_length is specified in the options or AJAX request, the result list is shortened to
        the correct offset and length.  Paged or not, the finalized object_list is then returned.
        """

        options = self._get_datatable_options()

        # Narrow the results to the appropriate page length for serialization
        if options['page_length'] != -1:
            i_begin = options['start_offset']
            i_end = options['start_offset'] + options['page_length']
            object_list = object_list[i_begin:i_end]

        return object_list

    def serialize_to_json(self, response_data):
        """ Returns the JSON string for the compiled data object. """

        indent = None
        if settings.DEBUG:
            indent = 4

        return json.dumps(response_data, indent=indent)

    def get_record_data(self, obj):
        """
        Returns a list of column data intended to be passed directly back to dataTables.js.

        Each column generates a 2-tuple of data. [0] is the data meant to be displayed to the client
        and [1] is the data in plain-text form, meant for manual searches.  One wouldn't want to
        include HTML in [1], for example.

        """

        options = self._get_datatable_options()

        data = {
            'DT_RowId': obj.pk,
        }
        for i, name in enumerate(options['columns']):
            column_data = self.get_column_data(i, name, obj)[0]
            if six.PY2 and isinstance(column_data, str):  # not unicode
                column_data = column_data.decode('utf-8')
            data[str(i)] = six.text_type(column_data)
        return data

    def get_column_data(self, i, name, instance):
        """ Finds the backing method for column ``name`` and returns the generated data. """
        column = get_field_definition(name)
        is_custom, f = self._get_resolver_method(i, column)
        if is_custom:
            args, kwargs = self._get_preloaded_data(instance)
            try:
                kwargs['default_value'] = self._get_column_data_default(instance, column)[1]
            except AttributeError:
                kwargs['default_value'] = None
            kwargs['field_data'] = name
            kwargs['view'] = self
            values = f(instance, *args, **kwargs)
        else:
            values = f(instance, column)

        if not isinstance(values, (tuple, list)):
            if six.PY2:
                if isinstance(values, str):  # not unicode
                    values = values.decode('utf-8')
                else:
                    values = unicode(values)
            values = (values, re.sub(r'<[^>]+>', '', six.text_type(values)))

        return values

    def preload_record_data(self, instance):
        """
        An empty hook for letting the view do something with ``instance`` before column lookups are
        called against the object.  The tuple of items returned will be passed as positional
        arguments to any of the ``get_column_FIELD_NAME_data()`` methods.

        """

        return ()

    def _get_preloaded_data(self, instance):
        """
        Fetches value from ``preload_record_data()``.

        If a single value is returned and it is not a dict, list or tuple, it is made into a tuple.
        The tuple will be supplied to the resolved method as ``*args``.

        If the returned value is already a list/tuple, it will also be sent as ``*args``.

        If the returned value is a dict, it will be sent as ``**kwargs``.

        The two types cannot be mixed.

        """
        preloaded_data = self.preload_record_data(instance)
        if isinstance(preloaded_data, dict):
            preloaded_args = ()
            preloaded_kwargs = preloaded_data
        elif isinstance(preloaded_data, (tuple, list)):
            preloaded_args = preloaded_data
            preloaded_kwargs = {}
        else:
            preloaded_args = (preloaded_data,)
            preloaded_kwargs = {}
        return preloaded_args, preloaded_kwargs

    def _get_resolver_method(self, i, column):
        """
        Using a slightly mangled version of the column's name (explained below) each column's value
        is derived.

        Each field can generate customized data by defining a method on the view called either
        ``get_column_FIELD_NAME_data`` or ``get_column_INDEX_data``.

        If the FIELD_NAME approach is used, the name is the raw field name (e.g.,
        ``"street_name"``) or else the friendly representation defined in a 2-tuple such as
        ``("Street name", "subdivision__home__street_name")``, where the name has non-alphanumeric
        characters stripped to single underscores. For example, the friendly name ``'Region:
        Subdivision Type'`` would convert to ``'Region_Subdivision_Type'``, requiring the method
        name ``get_column_Region_Subdivision_Type_data``.

        Alternatively, if the INDEX approach is used, a method will be fetched called
        ``get_column_0_data``, or otherwise using the 0-based index of the column's position as
        defined in the view's ``datatable_options['columns']`` setting.

        Finally, if a third element is defined in the tuple, it will be treated as the function or
        name of a member attribute which will be used directly.
        """

        callback = column.callback
        if callback:
            if callable(callback):
                return True, callback
            return True, getattr(self, callback)

        # Treat the 'nice name' as the starting point for looking up a method
        name = force_text(column.pretty_name, errors="ignore")
        if not name:
            name = column.fields[0]

        mangled_name = re.sub(r'[\W_]+', '_', name)

        f = getattr(self, 'get_column_%s_data' % mangled_name, None)
        if f:
            return True, f

        f = getattr(self, 'get_column_%d_data' % i, None)
        if f:
            return True, f

        return False, self._get_column_data_default

    def _get_column_data_default(self, instance, column, *args, **kwargs):
        """ Default mechanism for resolving ``column`` through the model instance ``instance``. """

        def chain_lookup(obj, bit):
            try:
                value = getattr(obj, bit)
            except (AttributeError, ObjectDoesNotExist):
                value = None
            else:
                if callable(value):
                    if isinstance(value, Manager):
                        pass
                    elif not hasattr(value, 'alters_data') or value.alters_data is not True:
                        value = value()
            return value

        values = []
        for field_name in column.fields:
            value = reduce(chain_lookup, [instance] + field_name.split('__'))

            if isinstance(value, Model):
                value = six.text_type(value)

            if value is not None:
                values.append(value)

        if len(values) == 1:
            value = values[0]
        else:
            value = u' '.join(map(six.text_type, values))

        return value, value


class LegacyDatatableView(LegacyDatatableMixin, ListView):
    """
    Implements :py:class:`LegacyDatatableMixin` and the standard Django ``ListView``.
    """


class LegacyConfigurationDatatableMixin(DatatableMixin):
    """
    Modern :py:class:`DatatableView` mechanisms simply powered by the old configuration style.  Use
    this if you can. If you get errors and you've been overriding things on the old DatatableView,
    fall back to using ``LegacyDatatableView``, which provides those old hooks.

    The :py:meth:`.get_datatable_options` hook is still respected as the getter for runtime
    configuration, but it will be broken up into keyword arguments to be forwarded to its
    :py:class:`~datatableview.datatables.Datatable`, which in this case is a
    :py:class:`~datatableview.datatables.LegacyDatatable`.
    """

    datatable_options = None
    datatable_class = LegacyDatatable

    def get_datatable_options(self):
        return self.datatable_options

    def _get_datatable_options(self):
        """ Helps to keep the promise that we only run ``get_datatable_options()`` once. """
        if not hasattr(self, '_datatable_options'):
            self._datatable_options = self.get_datatable_options()

            # Convert sources from list to tuple, so that modern Column tracking dicts can hold the
            # field definitions as keys.
            columns = self._datatable_options.get('columns', [])
            for i, column in enumerate(columns):
                if len(column) >= 2 and isinstance(column[1], list):
                    column = list(column)
                    column[1] = tuple(column[1])
                    columns[i] = tuple(column)

        return self._datatable_options

    def get_datatable_kwargs(self, **kwargs):
        kwargs.update({
            'object_list': self.get_queryset(),
            'view': self,
            'model': self.model,  # Potentially ``None``
            'callback_target': self,
        })

        # This is provided by default, but if the view is instantiated outside of the request cycle
        # (such as for the purposes of embedding that view's datatable elsewhere), the request may
        # not be required, so the user may not have a compelling reason to go through the trouble of
        # putting it on self.
        if hasattr(self, 'request'):
            kwargs['url'] = self.request.path
            kwargs['query_config'] = self.request.GET
        else:
            kwargs['query_config'] = {}

        kwargs.update(self._get_datatable_options())
        return kwargs

class LegacyConfigurationDatatableView(LegacyConfigurationDatatableMixin, ListView):
    """
    Implements :py:class:`LegacyConfigurationDatatableMixin` and the standard Django ``ListView``.
    """

