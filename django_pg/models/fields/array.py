from django.core.management.color import no_style
from django.db import models
from django_pg.utils.south import south_installed


class ArrayField(models.Field, metaclass=models.SubfieldBase):
    """Field for storing PostgreSQL arrays."""

    description = 'PostgreSQL arrays.'

    def __init__(self, of=models.IntegerField, **kwargs):
        # The `of` argument is a bit tricky once we need compatibility
        #   with South.
        # South can't store a field, and the eval it performs doesn't
        #   put enough things in the context to use South's internal
        #   "get field" function (`BaseMigration.gf`).
        # Therefore, we need to be able to accept a South triple of our
        #   sub-field and hook into South to get the correct thing
        #   back.
        if isinstance(of, tuple) and south_installed:
            from south.utils import ask_for_it_by_name as gf
            of = gf(of[0])(*of[1], **of[2])

        # Arrays in PostgreSQL are arrays of a particular type.
        # Save the subtype in our field class.
        self._of = of
        if isinstance(self._of, type):
            self._of = self._of()

        # Set "null" to True. Arrays don't have nulls, but null=True
        # in the ORM amounts to nothing in SQL (whereas null=False
        # corresponds to `NOT NULL`)
        kwargs['null'] = True

        # Now pass the rest of the work to the Field superclass.
        super().__init__(**kwargs)

    def create_type(self, connection):
        if hasattr(self._of, 'create_type'):
            return self._of.create_type(connection)
        return

    def create_type_sql(self, connection, style=no_style(),
                                          only_if_not_exists=False ):
        if hasattr(self._of, 'create_type_sql'):
            return self._of.create_type_sql(connection, style,
                                        only_if_not_exists=only_if_not_exists)
        return ''

    def db_type(self, connection):
        """Return the appropriate type to create a PostgreSQL array."""

        # Retrieve the SQL type of the sub-field.
        db_subfield = self._of.db_type(connection)

        # Return the PostgreSQL array type syntax.
        return '%s[]' % db_subfield

    def get_db_lookup_expression(self, lookup_type, value, connection):
        # If this is the "exact" lookup type, then explicitly
        # typecast our value to the proper type.
        if lookup_type == 'exact':
            return '{field} = {value}::%s' % self.db_type(connection)

        # If `__contains` was used to seek an item within the array,
        # return the appropriate PostgreSQL expression to handle that.
        if lookup_type == 'contains':
            if isinstance(value, (list, tuple)):
                return '{field} @> {value}::%s' % self.db_type(connection)
            return '{value} = ANY({field})'

        # If the lookup_type is "len", then we are asking for
        # an array of a given length.
        if lookup_type == 'len':
            return '{value} = ARRAY_LENGTH({field}, 1)'

    def get_db_prep_lookup(self, lookup_type, value, connection,
                            prepared=False):

        # Handle our special case: We don't want the "%" adding
        # to `contains` that comes with the Django stock implementation;
        # this is an array presence check, not a full text search.
        if lookup_type == 'contains':
            return [value]

        # Default behavior is fine in all other cases.
        return super().get_db_prep_lookup(lookup_type, value, connection,
                                            prepared=prepared, )

    def get_prep_lookup(self, lookup_type, value):
        # Handling for `__len`, which is a custom lookup type
        # for arrays, must be properly handled.
        if lookup_type == 'len':
            try:
                return int(value)
            except ValueError:
                raise TypeError('__len only supports integers.')

        # Arrays do not support many built-in lookups.
        if lookup_type not in ('exact', 'contains'):
            raise TypeError('Unsupported lookup type: %s' % lookup_type)

        # If we're checking on a list, coerce each individual value into
        # its appropriate lookup type.
        if isinstance(value, (list, tuple)):
            value = [self._of.get_prep_lookup('exact', i) for i in value]

        # The superclass handling is good enough for everything else.
        return super().get_prep_lookup(lookup_type, value)

    def get_prep_value(self, value):
        """Iterate over each item in the array, and run it
        through the `get_prep_value` of this array's type.
        """
        # If no valid value was given, return an empty list.
        if not value:
            return []

        # Appropriately coerce each individual value within
        # our array.
        answer = []
        for item in list(value):
            answer.append(self._of.get_prep_value(item))

        # Run the superclass' value coersion.
        return answer

    def register_composite(self, connection, globally=True):
        if hasattr(self._of, 'register_composite'):
            return self._of.register_composite(connection, globally=globally)
        return

    def south_field_triple(self):
        """Return a description of this field parsable by South."""

        # It's safe to import South at this point; this method
        # will never actually be called unless South is installed.
        from south.modelsinspector import introspector

        # Get the args and kwargs with which this field was generated.
        # The "double" variable name is a riff of of South "triples", since
        #   the `introspector` function only returns the final two elements
        #   of a South triple. This is fine since those two pieces are all
        #   we actually need.
        double = introspector(self._of)

        # Return the appropriate South triple.
        return (
            '%s.%s' % (self.__class__.__module__, self.__class__.__name__),
            [],
            {
                # The `of` argument is *itself* another triple, of
                #   the internal field.
                # The ArrayField constructor understands how to resurrect
                #   its internal field from this serialized state.
                'of': (
                    '{module}.{class_name}'.format(
                        module=self._of.__class__.__module__,
                        class_name=self._of.__class__.__name__,
                    ),
                    double[0],
                    double[1],
                ),
            },
        )

    def to_python(self, value):
        """Convert the database value to a Python list."""

        # We get lists back the vast majority of the time, because
        #   psycopg2 is awesome.
        # However, the individual items within the list may need to run
        #   through the `_of` field's `to_python`.
        if isinstance(value, list):
            return [self._of.to_python(i) for i in value]
