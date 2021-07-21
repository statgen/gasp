/* eslint-env jquery */
/* eslint no-unused-vars: ["error", { "vars": "local" }] */

function EditableElement(ele, useSup) {
    var edit_icon = $("<span>", {class: "label label-edit"})
        .append($("<a>", {class: "edit-pheno-modal"})
        .append($("<span>", {class: "glyphicon glyphicon-pencil", "aria-hidden": "true"})));
    $(ele).wrapInner($("<span>", {class: "current-value"}));
    if (useSup) {
        edit_icon = edit_icon.wrap($("<sup>")).parent();
    }
    $(ele).append(edit_icon);
    $value = $(ele).find(".current-value")

    this.setText = function(text) {
        if (text) {
            $value.text(text).removeClass("no-value")
        } else {
            $value.text("None").addClass("no-value")
        }
    }
    this.setText($value.text())
}

function init_editform(pheno_id, pheno_api_url) {
    var titleBox = new EditableElement("#pheno_main_title", true);
    var descBox = new EditableElement(".pheno-desc");
    var edit_form = new FormHelper("#editModalBox", "Job");

    $("a.edit-pheno-modal").click(function(evt) {
        evt.preventDefault();
        $.get(pheno_api_url).done(function(current_data) {
            var form_values = {name: current_data.name, description: current_data.description}
            edit_form.show_update_form(form_values, (new_values) => {
                return postFormData(pheno_api_url, new_values).then( () => {
                    titleBox.setText(new_values.name)
                    descBox.setText(new_values.description)
                });
            })
        })
    });
}

function init_new_job_button(selector, pheno_error) {
    selector = selector || "button[name=new_job]";
    if (!pheno_error) {
        $(selector).click(function(evt) {
            evt.preventDefault();
            var url = $(evt.target).data("link");
            document.location = url;
        });
    } else {
        $(selector).prop("title", pheno_error)
            .prop("disabled", true);
    }
}

function init_pheno_delete_button(selector) {
    selector = selector || "button[name=delete_pheno]";
    $(selector).click(function(evt) {
        evt.preventDefault();
        var url = $(evt.target).data("action");
        $("#deleteModal button.delete-pheno").data("action", url);
        $("#deleteModal").modal();
    });
    $("#deleteModal button.delete-pheno").click(function(evt) {
        evt.preventDefault();
        var url = $(evt.target).data("action");
        $.ajax({
            url: url, 
            type: "DELETE",
            success: function() {
                document.location.reload();
            },
            error: function() {
                alert("Deletion request failed");
            }
        });
    });
}

