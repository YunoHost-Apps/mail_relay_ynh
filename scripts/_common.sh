#!/bin/bash

readonly default_poll_interval_minutes=1
readonly service_description="Forward remote IMAP mail into a YunoHost mailbox"
readonly remote_credential_name="remote_password"
readonly local_imap_credential_name="local_imap_password"

deploy_sources() {
    mkdir -p "$install_dir"
    cp -a ../sources/. "$install_dir/"
    chmod 755 "$install_dir/email_forward.py"
    chown -R "$app:$app" "$install_dir"
}

ensure_log_dir() {
    mkdir -p "/var/log/$app"
    touch "/var/log/$app/$app.log"
    chmod 750 "/var/log/$app"
    chmod 640 "/var/log/$app/$app.log"
    chown -R "$app:$app" "/var/log/$app"
}

save_runtime_settings() {
    local resolved_target_email
    resolved_target_email="$(resolve_target_email)"
    if [ -z "$resolved_target_email" ]; then
        ynh_die --message="Unable to resolve the primary email address for YunoHost user $target_user"
    fi

    ynh_app_setting_set --key=remote_email --value="$remote_email"
    ynh_app_setting_set --key=remote_host --value="$remote_host"
    ynh_app_setting_set --key=remote_port --value="$remote_port"
    ynh_app_setting_set --key=delete_remote --value="$delete_remote"
    ynh_app_setting_set --key=target_user --value="$target_user"
    ynh_app_setting_set --key=local_imap_email --value="$resolved_target_email"
    ynh_app_setting_set --key=poll_interval_minutes --value="${poll_interval_minutes:-$default_poll_interval_minutes}"
}

credential_path() {
    local credential_name="$1"
    printf '%s/%s.cred' "$data_dir" "$credential_name"
}

store_password_credential() {
    local credential_name="$1"
    local secret_value="$2"
    local target_path
    target_path="$(credential_path "$credential_name")"

    printf '%s' "$secret_value" | systemd-creds encrypt --name="$credential_name" - "$target_path" >/dev/null
    chmod 600 "$target_path"
    chown root:root "$target_path"
}

assert_password_credential_exists() {
    local credential_name="$1"
    if [ ! -f "$(credential_path "$credential_name")" ]; then
        ynh_die --message="Missing encrypted password credential at $(credential_path "$credential_name")"
    fi
}

initialize_state() {
    local state_path="$data_dir/state.json"
    local now_utc
    now_utc="$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")"

    cat > "$state_path" <<EOF_STATE
{
  "last_transfer_at": "$now_utc",
  "last_uid": null,
  "uidvalidity": null
}
EOF_STATE

    chmod 600 "$state_path"
    chown "$app:$app" "$state_path"
}

initialize_state_if_missing() {
    if [ ! -f "$data_dir/state.json" ]; then
        initialize_state
    else
        chown "$app:$app" "$data_dir/state.json"
        chmod 600 "$data_dir/state.json"
    fi
}

register_service() {
    yunohost service add "$app" --description="$service_description" --log="/var/log/$app/$app.log"
}

resolve_target_email() {
    yunohost user info "$target_user" --output-as json 2>/dev/null \
        | tr -d '\n' \
        | sed -n 's/.*"mail"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}
