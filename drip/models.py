from datetime import datetime, timedelta
from django.db.models import Count, Min, Max, Sum, Avg

from django.db import models
from django.contrib.auth.models import User
from django.conf import settings

# just using this to parse, but totally insane package naming...
# https://bitbucket.org/schinckel/django-timedelta-field/
import timedelta as djangotimedelta


class Drip(models.Model):
    date = models.DateTimeField(auto_now_add=True)
    lastchanged = models.DateTimeField(auto_now=True)

    name = models.CharField(
        max_length=255,
        unique=True,
        verbose_name='Drip Name',
        help_text='A unique name for this drip.')

    enabled = models.BooleanField(default=False)

    subject_template = models.TextField(null=True, blank=True)
    if getattr(settings, 'DRIP_USE_CREATESEND', False):        
        body_html_template = models.TextField(null=True, blank=True,
                                              help_text='You may use createsend custom fields in the body')
    else:
        body_html_template = models.TextField(null=True, blank=True,
                                              help_text='You will have settings and user in the context.')

    @property
    def drip(self):
        from drip.drips import DripBase

        drip = DripBase(drip_model=self,
                        name=self.name,
                        subject_template=self.subject_template if self.subject_template else None,
                        body_template=self.body_html_template if self.body_html_template else None)
        return drip

    def __unicode__(self):
        return self.name


class SentDrip(models.Model):
    """
    Keeps a record of all sent drips.
    """
    date = models.DateTimeField(auto_now_add=True)

    drip = models.ForeignKey('drip.Drip', related_name='sent_drips')
    user = models.ForeignKey('auth.User', related_name='sent_drips')

    subject = models.TextField()
    body = models.TextField()



METHOD_TYPES = (
    ('filter', 'Filter'),
    ('exclude', 'Exclude'),
)

LOOKUP_TYPES = (
    ('exact', 'exactly'),
    ('iexact', 'exactly (case insensitive)'),
    ('contains', 'contains'),
    ('icontains', 'contains (case insensitive)'),
    ('regex', 'regex'),
    ('iregex', 'contains (case insensitive)'),
    ('gt', 'greater than'),
    ('gte', 'greater than or equal to'),
    ('lt', 'lesser than'),
    ('lte', 'lesser than or equal to'),
    ('startswith', 'starts with'),
    ('istartswith', 'starts with (case insensitive)'),
    ('endswith', 'ends with'),
    ('iendswith', 'ends with (case insensitive)'),
    ('isnull','is NULL'),
)

ANNOTATE_TYPES = (
    ('none', 'None'),
    ('sum','Sum'),
    ('count','Count'),
    ('min','Min'),
    ('max', 'Max'),
    ('avg', 'Average'),
    )


class BaseRule(models.Model):
    date = models.DateTimeField(auto_now_add=True)
    lastchanged = models.DateTimeField(auto_now=True)

    drip = models.ForeignKey(Drip)

    method_type = models.CharField(max_length=12, default='filter', choices=METHOD_TYPES)
    field_name = models.CharField(max_length=128, verbose_name='Field name off User')
    annotate   = models.CharField(max_length=32, choices=ANNOTATE_TYPES, default='none')
    lookup_type = models.CharField(max_length=12, default='exact', choices=LOOKUP_TYPES)

    field_value = models.CharField(max_length=255,
        help_text=('Can be anything from a number, to a string. Or, do ' +
                   '`now-7 days` or `now+3 days` for fancy timedelta.'))

    def apply(self, qs, now=datetime.now):
        if self.annotate != 'none':
            field_name = "%s__annotate%s" % (self.field_name, self.id)
            if self.annotate == 'sum':
                _kwargs = {
                    field_name: Sum(self.field_name)
                    }
            elif self.annotate == 'count':
                _kwargs = {
                    field_name: Count(self.field_name)
                    }
            elif self.annotate == 'min':
                _kwargs = {
                    field_name: Min(self.field_name)
                    }
            elif self.annotate == 'max':
                _kwargs = {
                    field_name: Max(self.field_name)
                    }
            elif self.annotate == 'avg':
                _kwargs = {
                    field_name: Avg(self.field_name)
                    }
            qs = qs.annotate(**_kwargs)
        else:
            field_name = self.field_name


        field_name = '__'.join([field_name, self.lookup_type])
        field_value = self.field_value

        # set time deltas and dates
        if field_value.startswith('now-'):
            field_value = self.field_value.replace('now-', '')
            delta = djangotimedelta.parse(field_value)
            field_value = now() - delta
        elif field_value.startswith('now+'):
            field_value = self.field_value.replace('now+', '')
            delta = djangotimedelta.parse(field_value)
            field_value = now() + delta

        # set booleans
        if field_value == 'True':
            field_value = True
        if field_value == 'False':
            field_value = False

        kwargs = {field_name: field_value}

        if self.method_type == 'filter':
            return qs.filter(**kwargs)
        elif self.method_type == 'exclude':
            return qs.exclude(**kwargs)

        # catch as default
        return qs.filter(**kwargs)

class QuerySetRule(BaseRule):
    pass
    

class SubqueryRule(BaseRule):
    app_name   = models.CharField(max_length=64, verbose_name='App where the model is stored')
    model_name = models.CharField(max_length=64, verbose_name='Model to subquery')
    user_field = models.CharField(max_length=128, verbose_name='Field name which is a foreign key to User', default='user')

class ExcludeSubqueryRule(BaseRule):
    app_name   = models.CharField(max_length=64, verbose_name='App where the model is stored')
    model_name = models.CharField(max_length=64, verbose_name='Model to subquery')
    user_field = models.CharField(max_length=128, verbose_name='Field name which is a foreign key to User', default='user')
