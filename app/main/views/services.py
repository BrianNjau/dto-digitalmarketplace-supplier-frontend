from flask_login import login_required, current_user
from flask import render_template, request, redirect, url_for, abort, flash, \
    current_app

from ... import data_api_client, flask_featureflags
from ...main import main, content_loader
from ..helpers.services import (
    is_service_modifiable, is_service_associated_with_supplier,
    get_service_attributes,
    get_draft_document_url, count_unanswered_questions,
    get_next_section_name
)
from ..helpers.frameworks import get_declaration_status, g_cloud_7_is_open_or_404

from dmutils.apiclient import APIError, HTTPError
from dmutils.formats import format_service_price
from dmutils import s3
from dmutils.documents import upload_service_documents


@main.route('/services')
@login_required
def list_services():
    template_data = main.config['BASE_TEMPLATE_DATA']
    suppliers_services = data_api_client.find_services(
        supplier_id=current_user.supplier_id
    )

    return render_template(
        "services/list_services.html",
        services=suppliers_services["services"],
        updated_service_id=request.args.get('updated_service_id'),
        updated_service_name=request.args.get('updated_service_name'),
        updated_service_status=request.args.get('updated_service_status'),
        **template_data), 200


#  #######################  EDITING LIVE SERVICES #############################


@main.route('/services/<string:service_id>', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('EDIT_SERVICE_PAGE')
def edit_service(service_id):
    service = data_api_client.get_service(service_id).get('services')

    if not is_service_associated_with_supplier(service):
        abort(404)

    return _update_service_status(service)


@main.route('/services/<string:service_id>', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('EDIT_SERVICE_PAGE')
def update_service_status(service_id):
    service = data_api_client.get_service(service_id).get('services')

    if not is_service_associated_with_supplier(service):
        abort(404)

    if not is_service_modifiable(service):
        return _update_service_status(
            service,
            "Sorry, but this service isn't modifiable."
        )

    # Value should be either public or private
    status = request.form.get('status', '').lower()

    translate_frontend_to_api = {
        'public': 'published',
        'private': 'enabled'
    }

    if status in translate_frontend_to_api.keys():
        status = translate_frontend_to_api[status]
    else:
        return _update_service_status(
            service,
            "Sorry, but '{}' is not a valid status.".format(status)
        )

    try:
        updated_service = data_api_client.update_service_status(
            service.get('id'), status,
            current_user.email_address)

    except APIError:

        return _update_service_status(
            service,
            "Sorry, there's been a problem updating the status."
        )

    updated_service = updated_service.get("services")
    return redirect(
        url_for(".list_services",
                updated_service_id=updated_service.get("id"),
                updated_service_name=updated_service.get("serviceName"),
                updated_service_status=updated_service.get("status"))
    )


@main.route('/services/<string:service_id>/edit/<string:section_id>', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('EDIT_SERVICE_PAGE')
def edit_section(service_id, section_id):
    service = data_api_client.get_service(service_id)
    if service is None:
        abort(404)
    service = service['services']

    if not is_service_associated_with_supplier(service):
        abort(404)

    content = content_loader.get_builder('g-cloud-6', 'edit_service').filter(service)
    section = content.get_section(section_id)
    if section is None or not section.editable:
        abort(404)

    return render_template(
        "services/edit_section.html",
        section=section,
        service_data=service,
        service_id=service_id,
        **main.config['BASE_TEMPLATE_DATA']
    )


@main.route('/services/<string:service_id>/edit/<string:section_id>', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('EDIT_SERVICE_PAGE')
def update_section(service_id, section_id):
    service = data_api_client.get_service(service_id)
    if service is None:
        abort(404)
    service = service['services']

    if not is_service_associated_with_supplier(service):
        abort(404)

    content = content_loader.get_builder('g-cloud-6', 'edit_service').filter(service)
    section = content.get_section(section_id)
    if section is None or not section.editable:
        abort(404)

    posted_data = section.get_data(request.form)

    try:
        data_api_client.update_service(
            service_id,
            posted_data,
            current_user.email_address)
    except HTTPError as e:
        errors = section.get_error_messages(e.message, service['lot'])
        if not posted_data.get('serviceName', None):
            posted_data['serviceName'] = service.get('serviceName', '')
        return render_template(
            "services/edit_section.html",
            section=section,
            service_data=posted_data,
            service_id=service_id,
            errors=errors,
            **main.config['BASE_TEMPLATE_DATA']
        )

    return redirect(url_for(".edit_service", service_id=service_id))


#  ####################  CREATING NEW DRAFT SERVICES ##########################


@main.route('/submission/<framework_slug>/create', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def start_new_draft_service(framework_slug):
    """
    Page to kick off creation of a new (G7) service.
    """
    # TODO add a test for 404 if framework doesn't exist
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    if framework['status'] != 'open':
        abort(404)
    template_data = main.config['BASE_TEMPLATE_DATA']

    question = content_loader.get_builder(framework_slug, 'edit_submission').get_question('lot')
    section = {
        "name": 'Create new service',
        "questions": [question]
    }

    return render_template(
        "services/edit_submission_section.html",
        framework=framework,
        question=question,
        service_data={},
        section=section,
        **dict(template_data)
    ), 200


@main.route('/submission/<framework_slug>/create', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def create_new_draft_service(framework_slug):
    """
    Hits up the data API to create a new draft (G7) service.
    """
    # TODO add a test for 404 if framework doesn't exist
    framework = data_api_client.get_framework(framework_slug)['frameworks']
    if framework['status'] != 'open':
        abort(404)
    lot = request.form.get('lot', None)

    if not lot:
        question = content_loader.get_builder(framework_slug, 'edit_submission').get_question('lot')
        section = {
            "name": 'Create new service',
            "questions": [question]
        }

        errors = {
            question['id']: {
                "input_name": question['id'],
                "question": question['question'],
                "message": "Answer is required"
            }
        }

        return render_template(
            "services/edit_submission_section.html",
            framework=framework,
            question=question,
            service_data={},
            section=section,
            errors=errors,
            **main.config['BASE_TEMPLATE_DATA']
        ), 400

    supplier_id = current_user.supplier_id
    user = current_user.email_address

    try:
        draft_service = data_api_client.create_new_draft_service(
            framework_slug, supplier_id, user, lot
        )

    except APIError as e:
        abort(e.status_code)

    draft_service = draft_service.get('services')
    content = content_loader.get_builder(framework_slug, 'edit_submission').filter({
        'lot': draft_service.get('lot')
    })

    return redirect(
        url_for(
            ".edit_service_submission",
            service_id=draft_service.get('id'),
            section_id=content.get_next_editable_section_id(),
            return_to_summary=1
        )
    )


@main.route('/submission/services/<string:service_id>/copy', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def copy_draft_service(service_id):
    framework = data_api_client.get_framework('g-cloud-7')['frameworks']
    if framework['status'] != 'open':
        abort(404)
    draft = data_api_client.get_draft_service(service_id).get('services')

    if not is_service_associated_with_supplier(draft):
        abort(404)

    try:
        draft_copy = data_api_client.copy_draft_service(
            service_id,
            current_user.email_address
        )['services']

    except APIError as e:
        abort(e.status_code)

    return redirect(url_for(".edit_service_submission",
                            service_id=draft_copy['id'],
                            section_id='service_name',
                            return_to_summary=1))


@main.route('/submission/services/<string:service_id>/complete', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def complete_draft_service(service_id):
    framework = data_api_client.get_framework('g-cloud-7')['frameworks']
    if framework['status'] != 'open':
        abort(404)
    draft = data_api_client.get_draft_service(service_id).get('services')

    if not is_service_associated_with_supplier(draft):
        abort(404)

    try:
        data_api_client.complete_draft_service(
            service_id,
            current_user.email_address
        )

    except APIError as e:
        abort(e.status_code)

    flash({'service_name': draft.get('serviceName')}, 'service_completed')

    return redirect(url_for(".framework_services",
                    framework_slug=framework['slug'],
                    service_completed=service_id,
                    lot=draft['lot'].lower()))


@main.route('/submission/services/<string:service_id>/delete', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def delete_draft_service(service_id):
    framework = data_api_client.get_framework('g-cloud-7')['frameworks']
    if framework['status'] != 'open':
        abort(404)
    draft = data_api_client.get_draft_service(service_id).get('services')

    if not is_service_associated_with_supplier(draft):
        abort(404)

    if request.form.get('delete_confirmed', None) == 'true':
        try:
            data_api_client.delete_draft_service(
                service_id,
                current_user.email_address
            )
        except APIError as e:
            abort(e.status_code)

        flash({'service_name': draft.get('serviceName')}, 'service_deleted')
        return redirect(url_for(".framework_services", framework_slug=framework['slug']))
    else:
        return redirect(url_for(".view_service_submission",
                                service_id=service_id,
                                delete_requested=True)
                        )


@main.route('/submission/documents/<string:framework_slug>/<int:supplier_id>/<string:document_name>', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def service_submission_document(framework_slug, supplier_id, document_name):
    if current_user.supplier_id != supplier_id:
        abort(404)

    uploader = s3.S3(current_app.config['DM_G7_DRAFT_DOCUMENTS_BUCKET'])
    s3_url = get_draft_document_url(uploader,
                                    "{}/{}/{}".format(framework_slug, supplier_id, document_name))
    if not s3_url:
        abort(404)

    return redirect(s3_url)


@main.route('/submission/services/<string:service_id>', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def view_service_submission(service_id):
    framework = data_api_client.get_framework('g-cloud-7')['frameworks']
    try:
        data = data_api_client.get_draft_service(service_id)
        draft, last_edit = data['services'], data['auditEvents']
    except HTTPError as e:
        abort(e.status_code)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    draft['priceString'] = format_service_price(draft)
    content = content_loader.get_builder('g-cloud-7', 'edit_submission').filter(draft)

    sections = get_service_attributes(draft, content)

    unanswered_required, unanswered_optional = count_unanswered_questions(sections)
    delete_requested = True if request.args.get('delete_requested') else False

    return render_template(
        "services/service_submission.html",
        service_id=service_id,
        service_data=draft,
        last_edit=last_edit,
        sections=sections,
        unanswered_required=unanswered_required,
        unanswered_optional=unanswered_optional,
        delete_requested=delete_requested,
        declaration_status=get_declaration_status(data_api_client, 'g-cloud-7'),
        g7_status=framework.get('status'),
        deadline=current_app.config['G7_CLOSING_DATE'],
        **main.config['BASE_TEMPLATE_DATA']), 200


@main.route('/submission/services/<string:service_id>/edit/<string:section_id>', methods=['GET'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def edit_service_submission(service_id, section_id):
    framework = data_api_client.get_framework('g-cloud-7')['frameworks']
    if framework['status'] != 'open':
        abort(404)
    try:
        draft = data_api_client.get_draft_service(service_id)['services']
    except HTTPError as e:
        abort(e.status_code)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_builder('g-cloud-7', 'edit_submission').filter(draft)
    section = content.get_section(section_id)
    if section is None or not section.editable:
        abort(404)

    draft = section.unformat_data(draft)

    return render_template(
        "services/edit_submission_section.html",
        section=section,
        framework=framework,
        next_section_name=get_next_section_name(content, section_id),
        service_data=draft,
        service_id=service_id,
        return_to_summary=bool(request.args.get('return_to_summary')),
        **main.config['BASE_TEMPLATE_DATA']
    )


@main.route('/submission/services/<string:service_id>/edit/<string:section_id>', methods=['POST'])
@login_required
@flask_featureflags.is_active_feature('GCLOUD7_OPEN')
def update_section_submission(service_id, section_id):
    framework = data_api_client.get_framework('g-cloud-7')['frameworks']
    if framework['status'] != 'open':
        abort(404)
    try:
        draft = data_api_client.get_draft_service(service_id)['services']
    except HTTPError as e:
        abort(e.status_code)

    if not is_service_associated_with_supplier(draft):
        abort(404)

    content = content_loader.get_builder('g-cloud-7', 'edit_submission').filter(draft)
    section = content.get_section(section_id)
    if section is None or not section.editable:
        abort(404)

    errors = None
    update_data = section.get_data(request.form)

    uploader = s3.S3(current_app.config['DM_G7_DRAFT_DOCUMENTS_BUCKET'])
    documents_url = url_for('.dashboard', _external=True) + '/submission/documents/'
    uploaded_documents, document_errors = upload_service_documents(
        uploader, documents_url, draft, request.files, section,
        public=False)

    if document_errors:
        errors = section.get_error_messages(document_errors, draft['lot'])
    else:
        update_data.update(uploaded_documents)

    if not errors and section.has_changes_to_save(draft, update_data):
        try:
            data_api_client.update_draft_service(
                service_id,
                update_data,
                current_user.email_address,
                page_questions=section.get_field_names()
            )
        except HTTPError as e:
            update_data = section.unformat_data(update_data)
            errors = section.get_error_messages(e.message, draft['lot'])

    if errors:
        if not update_data.get('serviceName', None):
            update_data['serviceName'] = draft.get('serviceName', '')
        return render_template(
            "services/edit_submission_section.html",
            framework=framework,
            section=section,
            next_section_name=get_next_section_name(content, section_id),
            service_data=update_data,
            service_id=service_id,
            return_to_summary=bool(request.args.get('return_to_summary')),
            errors=errors,
            **main.config['BASE_TEMPLATE_DATA']
            )

    return_to_summary = bool(request.args.get('return_to_summary'))
    next_section = content.get_next_editable_section_id(section_id)

    if next_section and not return_to_summary and request.form.get('continue_to_next_section'):
        return redirect(url_for(".edit_service_submission", service_id=service_id, section_id=next_section))
    else:
        return redirect(url_for(".view_service_submission", service_id=service_id))


def _update_service_status(service, error_message=None):

    template_data = main.config['BASE_TEMPLATE_DATA']
    status_code = 400 if error_message else 200

    content = content_loader.get_builder('g-cloud-6', 'edit_service').filter(service)

    question = {
        'question': 'Choose service status',
        'hint': 'Private services don\'t appear in search results '
                'and don\'t have a URL',
        'name': 'status',
        'type': 'radio',
        'inline': True,
        'value': "Public" if service['status'] == 'published' else "Private",
        'options': [
            {
                'label': 'Public'
            },
            {
                'label': 'Private'
            }
        ]
    }

    return render_template(
        "services/service.html",
        service_id=service.get('id'),
        service_data=service,
        sections=get_service_attributes(service, content),
        error=error_message,
        **dict(question, **template_data)), status_code
