"""
Certificates Data Model:

course.certificates: {
    'certificates': [
        {
            'version': 1, // data contract version
            'id': 12345, // autogenerated identifier
            'name': 'Certificate 1',
            'description': 'Certificate 1 Description',
            'signatories': [
                {
                    'id': 24680, // autogenerated identifier
                    'name': 'Dr. Bob Smith',
                    'title': 'Dean of the College'
                }
            ]
        }
    ]
}
"""
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django_future.csrf import ensure_csrf_cookie
from django.http import HttpResponse
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_http_methods

from contentstore.utils import reverse_course_url
from edxmako.shortcuts import render_to_response
from opaque_keys.edx.keys import CourseKey, AssetKey
from student.auth import has_studio_read_access
from util.db import generate_int_id, MYSQL_MAX_INT
from util.json_request import JsonResponse
from xmodule.modulestore import EdxJSONEncoder
from xmodule.modulestore.django import modulestore
from contentstore.views.assets import delete_asset
from contentstore.views.exception import CertificateValidationError
from django.core.exceptions import PermissionDenied

CERTIFICATE_SCHEMA_VERSION = 1
CERTIFICATE_MINIMUM_ID = 100


def _get_course_and_check_access(course_key, user, depth=0):
    """
    Internal method used to calculate and return the locator and
    course module for the view functions in this file.
    """
    if not has_studio_read_access(user, course_key):
        raise PermissionDenied()
    course_module = modulestore().get_course(course_key, depth=depth)
    return course_module


def _delete_asset(course_key, asset_key_string):
    """
    Internal method used to create asset key from string and
    remove asset by calling delete_asset method of assets module.
    """
    if asset_key_string:
        asset_key = AssetKey.from_string(asset_key_string)
        delete_asset(course_key, asset_key)


class CertificateManager(object):
    """
    The CertificateManager is responsible for storage, retrieval, and manipulation of Certificates
    Certificates are not stored in the Django ORM, they are a field/setting on the course descriptor
    """
    @staticmethod
    def parse(json_string):
        """
        Deserialize the provided JSON data into a standard Python object
        """
        try:
            certificate = json.loads(json_string)
        except ValueError:
            raise CertificateValidationError(_("invalid JSON"))
        # Include the data contract version
        certificate["version"] = CERTIFICATE_SCHEMA_VERSION
        # Ensure a signatories list is always returned
        if certificate.get("signatories") is None:
            certificate["signatories"] = []
        return certificate

    @staticmethod
    def validate(certificate_data):
        """
        Ensure the certificate data contains all of the necessary fields and the values match our rules
        """
        # Ensure the schema version meets our expectations
        if certificate_data.get("version") != CERTIFICATE_SCHEMA_VERSION:
            raise TypeError(
                "Unsupported certificate schema version: {0}.  Expected version: {1}.".format(
                    certificate_data.get("version"),
                    CERTIFICATE_SCHEMA_VERSION
                )
            )
        if not certificate_data.get("name"):
            raise CertificateValidationError(_("must have name of the certificate"))

    @staticmethod
    def get_used_ids(course):
        """
        Return a list of certificate identifiers that are already in use for this course
        """
        if not course.certificates or not course.certificates.get('certificates'):
            return []
        return [cert['id'] for cert in course.certificates['certificates']]

    @staticmethod
    def assign_id(course, certificate_data, certificate_id=None):
        """
        Assign an identifier to the provided certificate data.
        If the caller did not provide an identifier, we autogenerate a unique one for them
        In addition, we check the certificate's signatories and ensure they also have unique ids
        """
        used_ids = CertificateManager.get_used_ids(course)
        if certificate_id:
            certificate_data['id'] = int(certificate_id)
        else:
            certificate_data['id'] = generate_int_id(
                CERTIFICATE_MINIMUM_ID,
                MYSQL_MAX_INT,
                used_ids
            )

        for index, signatory in enumerate(certificate_data['signatories']):  # pylint: disable=unused-variable
            if signatory and not signatory.get('id', False):
                signatory['id'] = generate_int_id(used_ids=used_ids)
            used_ids.append(signatory['id'])

        return certificate_data

    @staticmethod
    def to_json(certificate):
        """
        Serialize the Certificate object's locally-stored certificate data to a JSON representation
        We use direct access here for specific keys in order to enforce their presence
        """
        # pylint: disable=no-member
        certificate_data = certificate.get_certificate_data()
        return {
            "id": certificate_data['id'],
            "name": certificate_data['name'],
            "description": certificate_data['description'],
            "version": CERTIFICATE_SCHEMA_VERSION,
            "signatories": certificate_data['signatories']
        }

    @staticmethod
    def from_json(course, value):
        """
        Deserialize from a JSON representation into a Certificate object.
        'value' should be either a Certificate instance, or a valid JSON string
        """

        # Ensure the schema fieldset meets our expectations
        for key in ("name", "description", "version"):
            if key not in value:
                raise CertificateValidationError(_("Certificate dict {0} missing value key '{1}'").format(value, key))

        # Load up the Certificate data
        certificate_data = CertificateManager.parse(value)
        CertificateManager.validate(certificate_data)
        certificate_data = CertificateManager.assign_id(course, certificate_data, certificate_data.get('id', None))
        certificate = Certificate(course, certificate_data)

        # Return a new Certificate object instance
        return certificate

    @staticmethod
    def deserialize_certificate(course, json_string):
        """
        Returns a Certificate object instance given a valid course and JSON representation
        """
        return CertificateManager.from_json(course, json_string)

    @staticmethod
    def get_certificates(course):
        """
        Retrieve the certificates list from the provided course
        """
        # The top-level course field is 'certificates'
        course_certificates_field = getattr(course, 'certificates')
        # Inside of this top-level field are various things, including the actual 'certificates' list
        certificates = course_certificates_field.get('certificates', [])
        return certificates

    @staticmethod
    def remove_certificate(request, store, course, certificate_id):
        """
        Remove certificate from the course
        """
        for index, cert in enumerate(course.certificates['certificates']):
            if int(cert['id']) == int(certificate_id):
                course.certificates['certificates'].pop(index)
                store.update_item(course, request.user.id)
                CertificateManager.remove_signatory_signature_images(course, cert)
                break
        return JsonResponse(status=204)

    # pylint-disable: unused-variable
    @staticmethod
    def remove_signatory(request, store, course, certificate_id, signatory_id):
        """
        Remove the specified signatory from the provided course certificate
        """
        for cert_index, cert in enumerate(course.certificates['certificates']):  # pylint: disable=unused-variable
            if int(cert['id']) == int(certificate_id):
                for sig_index, signatory in enumerate(cert.get('signatories')):  # pylint: disable=unused-variable
                    if int(signatory_id) == int(signatory['id']):
                        _delete_asset(course.id, signatory['signature_image_path'])
                        del cert['signatories'][sig_index]
                        store.update_item(course, request.user.id)
                        break
        return JsonResponse(status=204)

    @staticmethod
    def remove_signatory_signature_images(course, certificate):
        """
        Remove the signature images for all signatories in specified certificate
        """
        for sig_index, signatory in enumerate(certificate.get('signatories')):  # pylint: disable=unused-variable
            _delete_asset(course.id, signatory['signature_image_path'])


class Certificate(object):
    """
    The logical representation of an individual course certificate
    """
    def __init__(self, course, certificate_data):
        """
        Instantiate a Certificate object instance using the provided information.
        """
        self.course = course
        self.certificate_data = certificate_data
        self.id = certificate_data['id']  # pylint: disable=invalid-name

    def get_certificate_data(self):
        """
        Retrieve the locally-stored certificate data from the Certificate object via a helper method
        """
        return self.certificate_data


@login_required
@require_http_methods(("GET", "POST"))
@ensure_csrf_cookie
def certificates_list_handler(request, course_key_string):
    """
    A RESTful handler for Course Certificates

    GET
        html: return Certificates list page (Backbone application)
    POST
        json: create new Certificate
    """
    course_key = CourseKey.from_string(course_key_string)
    store = modulestore()
    with store.bulk_operations(course_key):
        try:
            course = _get_course_and_check_access(course_key, request.user)
        except PermissionDenied:
            msg = _('PermissionDenied: Failed in authenticating {user}').format(user=request.user)
            return JsonResponse({"error": msg}, status=403)

        if 'text/html' in request.META.get('HTTP_ACCEPT', 'text/html'):
            certificate_url = reverse_course_url('certificates.certificates_list_handler', course_key)
            course_outline_url = reverse_course_url('course_handler', course_key)
            upload_asset_url = reverse_course_url('assets_handler', course_key)
            certificates = None
            print 'VIEW SETTING: {}'.format(settings.FEATURES.get('CERTIFICATES_HTML_VIEW'))
            if settings.FEATURES.get('CERTIFICATES_HTML_VIEW', False):
                certificates = CertificateManager.get_certificates(course)

            print certificates
            return render_to_response('certificates.html', {
                'context_course': course,
                'certificate_url': certificate_url,
                'course_outline_url': course_outline_url,
                'upload_asset_url': upload_asset_url,
                'certificates': json.dumps(certificates),
            })
        elif "application/json" in request.META.get('HTTP_ACCEPT'):
            # Retrieve the list of certificates for the specified course
            if request.method == 'GET':
                certificates = CertificateManager.get_certificates(course)
                return JsonResponse(certificates, encoder=EdxJSONEncoder)
            elif request.method == 'POST':
                # Add a new certificate to the specified course
                try:
                    new_certificate = CertificateManager.deserialize_certificate(course, request.body)
                except CertificateValidationError as err:
                    return JsonResponse({"error": err.message}, status=400)
                if course.certificates.get('certificates') is None:
                    course.certificates['certificates'] = []
                course.certificates['certificates'].append(new_certificate.get_certificate_data())
                response = JsonResponse(CertificateManager.to_json(new_certificate), status=201)
                response["Location"] = reverse_course_url(
                    'certificates.certificates_detail_handler',
                    course.id,
                    kwargs={'certificate_id': new_certificate.id}  # pylint: disable=no-member
                )
                store.update_item(course, request.user.id)
                course = _get_course_and_check_access(course_key, request.user)
                print course.certificates['certificates']
                return response
        else:
            return HttpResponse(status=406)


@login_required
@ensure_csrf_cookie
@require_http_methods(("POST", "PUT", "DELETE"))
def certificates_detail_handler(request, course_key_string, certificate_id):
    """
    JSON API endpoint for manipulating a course certificate via its internal identifier.
    Utilized by the Backbone.js 'certificates' application model

    POST or PUT
        json: update the specified certificate based on provided information
    DELETE
        json: remove the specified certificate from the course
    """
    course_key = CourseKey.from_string(course_key_string)
    store = modulestore()
    with store.bulk_operations(course_key):
        course = _get_course_and_check_access(course_key, request.user)
        certificates_list = course.certificates.get('certificates', [])

        match_index = None
        match_cert = None
        for index, cert in enumerate(certificates_list):
            if certificate_id is not None:
                if int(cert['id']) == int(certificate_id):
                    match_index = index
                    match_cert = cert

        if request.method in ('POST', 'PUT'):
            try:
                new_certificate = CertificateManager.deserialize_certificate(course, request.body)
            except CertificateValidationError as err:
                return JsonResponse({"error": err.message}, status=400)

            if match_cert:
                certificates_list[match_index] = CertificateManager.to_json(new_certificate)
            else:
                certificates_list.append(CertificateManager.to_json(new_certificate))

            store.update_item(course, request.user.id)
            return JsonResponse(CertificateManager.to_json(new_certificate), status=201)

        elif request.method == "DELETE":
            if not match_cert:
                return JsonResponse(status=404)

            return CertificateManager.remove_certificate(
                request=request,
                store=store,
                course=course,
                certificate_id=certificate_id
            )


@login_required
@ensure_csrf_cookie
@require_http_methods(("POST", "PUT", "DELETE"))
def signatory_detail_handler(request, course_key_string, certificate_id, signatory_id):
    """
    JSON API endpoint for manipulating a specific course certificate signatory via its internal identifier.
    Utilized by the Backbone 'certificates' application.

    DELETE
        json: Remove the specified signatory from the specified certificate
    """
    course_key = CourseKey.from_string(course_key_string)
    store = modulestore()
    with store.bulk_operations(course_key):
        course = _get_course_and_check_access(course_key, request.user)
        certificates_list = course.certificates['certificates']

        match_cert = None
        # pylint: disable=unused-variable
        for index, cert in enumerate(certificates_list):
            if certificate_id is not None:
                if int(cert['id']) == int(certificate_id):
                    match_cert = cert

        if request.method == "DELETE":
            if not match_cert:
                return JsonResponse(status=404)

            return CertificateManager.remove_signatory(
                request=request,
                store=store,
                course=course,
                certificate_id=certificate_id,
                signatory_id=signatory_id
            )
