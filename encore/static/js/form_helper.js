
function FormHelper(source_id, item_name) {
    var $modal = $(source_id);
    var resolve = null;
    var actionCallBack = null;
    var itemName = item_name || "Item";
    
    var inputs = {} 
    $modal.find("input,textarea").each((idx, ele) => {
        var $ele = $(ele);
        if($ele.data("bind")) {
            inputs[$ele.data("bind")] = $ele;
        }
    });
    var ui = {
        "title": $modal.find(".modal-title"), 
        "action": $modal.find(".modal-action"),
        "error_message": $modal.find(".error-message")
    }

    $modal.find("form").on("keyup keypress", function(e) {
        // prevent submisson on "Enter" press
        var keyCode = e.keyCode || e.which;
        if (keyCode === 13) { 
            e.preventDefault();
            return false;
        }
    });
    $modal.on("shown.bs.modal", () => {
        this.remove_error();
        $modal.find("[data-focus]").focus();
    });

    ui.action.on("click", (evt) => {
        if(resolve) {
            if (actionCallBack) {
                this.remove_error();
                var result = actionCallBack( this.get_values() );
                result
                    .then( (value) => {
                        resolve({done: true, value});
                        resolve = null;
                        $modal.modal('hide');
                    })
                    .catch( (err) => {
                        this.show_error(err)
                    })
            } else {
                resolve({done: true, noaction: true});
            }
        }
    })
    $modal.on("hidden.bs.modal", () => {
        if(resolve) {
            resolve({canceled: true});
            resolve = null;
        }
        this.remove_error();
    });

    this.set_values = function(vals) {
        for(var key of Object.keys(vals)) {
            var ele = inputs[key];
            if (ele) {
                if (ele.is(":checkbox")) {
                    ele.prop("checked", !!vals[key]);
                } else {
                    ele.val(vals[key] || '');
                }
            } else {
                console.warn(`Input for ${key} not found`)
            }
        }
    }
    this.get_values = function() {
        var vals = {}
        Object.keys(inputs).forEach( x => {
            var ele = inputs[x];
            var fval = null;
            if (ele.is(":checkbox")) {
                fval = +ele.is(":checked")
            } else {
                fval = ele.val()
            }
            vals[x] = fval
        } );
        return vals;
    }
    this.set_text = function(vals) {
        ui.title.text(vals.title || itemName)
        ui.action.text(vals.action || "Go")
    }
    this.return_promise = function(cb) {
        actionCallBack = cb;
        return new Promise((res, rej) => {
            resolve = res
        });
    }

    this.remove_error = function() {
        ui.error_message.hide();
    }
    this.show_error = function(err) {
        ui.error_message.text(err.user_details || err.details || err);
        ui.error_message.show();
    }

    this.show_add_form = function(vals, cb) {
        this.set_text({"title": "New " + itemName, "action": "Add"});
        this.set_values(vals);
        $modal.modal();
        return this.return_promise(cb)
    }
    this.show_update_form = function(vals, cb) {
        this.set_text({"title": "Update " + itemName, "action": "Update"});
        this.set_values(vals || {});
        $modal.modal();
        return this.return_promise(cb)
    }
}



function postFormData(url, data) {
    var body = new FormData()
    for(var key of Object.keys(data)) {
        body.append(key, data[key])
    }
    return fetch(url, {method: 'post', body: body}).then( (resp) => {
        if (!resp.ok) {
            return resp.json()
                .then( (errresp) => ( Promise.reject(errresp) ))
        }
        return resp.json();
    })
}
