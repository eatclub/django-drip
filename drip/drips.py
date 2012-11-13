from django.conf import settings
from datetime import datetime

from django.contrib.auth.models import User
from django.template import Context, Template
from drip.models import SentDrip, QuerySetRule, SubqueryRule, ExcludeSubqueryRule
from django.core.mail import EmailMultiAlternatives
from django.db.models.loading import get_model



class DripBase(object):
    """
    A base object for defining a Drip.

    You can extend this manually, or you can create full querysets
    and templates from the admin.
    """
    #: needs a unique name
    name = None
    subject_template = None
    body_template = None

    def __init__(self, drip_model, *args, **kwargs):
        self.drip_model = drip_model

        self.name = kwargs.pop('name', self.name)

        self.subject_template = kwargs.pop('subject_template', self.subject_template)
        self.body_template = kwargs.pop('body_template', self.body_template)

        if not self.name:
            raise AttributeError('You must define a name.')

        self.now_shift_kwargs = kwargs.get('now_shift_kwargs', {})


    #########################
    ### DATE MANIPULATION ###
    #########################

    def now(self):
        """
        This allows us to override what we consider "now", making it easy
        to build timelines of who gets what when.
        """
        return datetime.now() + self.timedelta(**self.now_shift_kwargs)

    def timedelta(self, *a, **kw):
        """
        If needed, this allows us the ability to manipuate the slicing of time.
        """
        from datetime import timedelta
        return timedelta(*a, **kw)

    def walk(self, into_past=0, into_future=0):
        """
        Walk over a date range and create new instances of self with new ranges.
        """
        walked_range = []
        for shift in range(-into_past, into_future):
            kwargs = dict(drip_model=self.drip_model,
                          name=self.name,
                          now_shift_kwargs={'days': shift})
            walked_range.append(self.__class__(**kwargs))
        return walked_range

    def apply_queryset_rules(self, qs):
        for queryset_rule in QuerySetRule.objects.filter(drip=self.drip_model):
            qs = queryset_rule.apply(qs, now=self.now)

        for app_model_name in SubqueryRule.objects.filter(drip=self.drip_model).values('model_name', 'app_name', 'user_field').distinct():
            model=get_model(app_model_name['app_name'], app_model_name['model_name'])
            
            model_qs = model.objects.values_list(app_model_name['user_field'], flat=True).distinct()
            for subquery_rule in SubqueryRule.objects.filter(drip=self.drip_model)\
                                                     .filter(app_name=app_model_name['app_name'])\
                                                     .filter(model_name=app_model_name['model_name'])\
                                                     .filter(user_field=app_model_name['user_field']):
                model_qs = subquery_rule.apply(model_qs, now=self.now)

            user_ids = model_qs
            qs = qs.filter(id__in=list(user_ids))

        for app_model_name in ExcludeSubqueryRule.objects.filter(drip=self.drip_model).values('model_name', 'app_name', 'user_field').distinct():
            model=get_model(app_model_name['app_name'], app_model_name['model_name'])
            
            model_qs = model.objects.values_list(app_model_name['user_field'], flat=True).distinct()
            for exclude_subquery_rule in ExcludeSubqueryRule.objects\
                                                            .filter(drip=self.drip_model)\
                                                            .filter(app_name=app_model_name['app_name'])\
                                                            .filter(model_name=app_model_name['model_name'])\
                                                            .filter(user_field=app_model_name['user_field']):
                model_qs = exclude_subquery_rule.apply(model_qs, now=self.now)

            user_ids = model_qs
            qs = qs.exclude(id__in=list(user_ids))
            
        return qs

    ##################
    ### MANAGEMENT ###
    ##################

    def get_queryset(self):
        try:
            return self._queryset
        except AttributeError:
            self._queryset = self.apply_queryset_rules(self.queryset())
            return self._queryset

    def run(self):
        """
        Get the queryset, prune sent people, and send it.
        """
        if not self.drip_model.enabled:
            return None

        self.prune()
        count = self.send()

        return count

    def prune(self):
        """
        Do an exclude for all Users who have a SentDrip already.
        """
        target_user_ids = self.get_queryset().values_list('id', flat=True)
        exclude_user_ids = SentDrip.objects.filter(date__lt=datetime.now(),
                                                   drip=self.drip_model,
                                                   user__id__in=target_user_ids)\
                                           .values_list('user_id', flat=True)
        self._queryset = self.get_queryset().exclude(id__in=exclude_user_ids)

    def build_email(self, user, send=False):
        """
        Creates Email instance and optionally sends to user.
        """
        use_createsend = getattr(settings, 'DRIP_USE_CREATESEND', False)

        from django.utils.html import strip_tags

        from_email = getattr(settings, 'DRIP_FROM_EMAIL', settings.EMAIL_HOST_USER)
        if use_createsend:
            context = Context({'user': user})
        else:
            context = Context()
        subject = Template(self.subject_template).render(context)
        body = Template(self.body_template).render(context)
        plain = strip_tags(body)

        email = EmailMultiAlternatives(subject, plain, from_email, [user.email])

        # check if there are html tags in the rendered template
        if len(plain) != len(body):
            email.attach_alternative(body, 'text/html')

        if send and not use_createsend:
            sd = SentDrip.objects.create(
                drip=self.drip_model,
                user=user,
                subject=subject,
                body=body
            )
            #email.send()

        return email

    def send(self):
        if getattr(settings, 'DRIP_USE_CREATESEND', False):
            template_name = 'Drip Template'
            segment_name = 'Drip Segment %s' % self.drip_model.name.replace("'",'').replace('"','')
            from createsend import Campaign, Segment,  CreateSend, BadRequest, Client

            CreateSend.api_key = settings.CREATESEND_API

            client = Client(settings.CREATESEND_CLIENT_ID)

            template_id = None
            for template in client.templates():
                if template.Name == template_name:
                    template_id = template.TemplateID

            if template_id is None:
                raise Exception("Template with the name '%s' does not exist" % template_name)

            segment_id = None
            for segment in client.segments():
                if segment.Title == segment_name:
                    segment_id = segment.SegmentID

            rules = []
            count = 0

            qs = self.get_queryset()
            clauses = []

            for user in qs:
                clauses.append("EQUALS %s" % user.email)
                count += 1
            rules = [{
                    
                    'Subject' : 'EmailAddress',
                    'Clauses' : clauses,
                    }]


            if count:
                if segment_id is not None:
                    segment = Segment(segment_id)
                    segment.clear_rules()
                    segment.update(segment_name, rules)
                else:
                    segment_id = Segment().create(settings.CREATESEND_LIST_ID, segment_name, rules)
                    segment = Sedment(segment_id)

                subject = Template(self.subject_template).render(Context())
                body = Template(self.body_template).render(Context())
                name    = 'Drip Campaign %s %s' % (self.drip_model.name, datetime.now().isoformat())

                from_address = getattr(settings, 'DRIP_FROM_EMAIL', settings.EMAIL_HOST_USER)

                template_content = {
                    "Multilines" : [{
                        'Content': body,
                        },],
                    }


                campaign_id = Campaign().create_from_template(settings.CREATESEND_CLIENT_ID, 
                                                              subject, 
                                                              name, 
                                                              from_address, 
                                                              from_address, 
                                                              from_address, 
                                                              [], 
                                                              [segment.details().SegmentID], 
                                                              template_id, 
                                                              template_content,
                                                              )
                campaign = Campaign(campaign_id)
                failed = False
                try:
                    campaign.send(settings.CREATESEND_CONFIRMATION_EMAIL)
                except BadRequest as br:
                    print "ERROR: Could not send Drip %s: %s" % (self.drip_model.name, br)
                    failed = True
                
                if not failed:
                    for user in qs:
                        sd = SentDrip.objects.create(
                            drip=self.drip_model,
                            user=user,
                            subject=subject,
                            body=body
                            )

            return count

            

        else:
            """
            Send the email to each user on the queryset.

            Add that user to the SentDrip.

            Returns a list of created SentDrips.
            """

            count = 0
            for user in self.get_queryset():
                msg = self.build_email(user, send=True)
                count += 1

            return count


    ####################
    ### USER DEFINED ###
    ####################

    def queryset(self):
        """
        Returns a queryset of auth.User who meet the
        criteria of the drip.

        Alternatively, you could create Drips on the fly
        using a queryset builder from the admin interface...
        """
        return User.objects
