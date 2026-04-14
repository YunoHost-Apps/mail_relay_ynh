#!/bin/bash

readonly default_poll_interval_minutes=1
readonly service_description="Forward remote IMAP mail into a YunoHost mailbox"
readonly runtime_config_path="$data_dir/config.yml"
readonly timer_unit_path="/etc/systemd/system/$app.timer"

deploy_sources() {
    mkdir -p "$install_dir"
    cp -a ../sources/. "$install_dir/"
    chown -R "$app:$app" "$install_dir"
    find "$install_dir" -type d -exec chmod 750 {} +
    find "$install_dir" -type f -exec chmod 640 {} +
    chmod 750 "$install_dir/email_forward.py"
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
    local current_remote_password
    local current_local_imap_password

    resolved_target_email="$(resolve_target_email)"
    if [ -z "$resolved_target_email" ]; then
        ynh_die --message="Unable to resolve the primary email address for YunoHost user $target_user"
    fi

    current_remote_password="${remote_password:-$(ynh_app_setting_get --key=remote_password)}"
    current_local_imap_password="${local_imap_password:-$(ynh_app_setting_get --key=local_imap_password)}"

    ynh_app_setting_set --key=remote_email --value="$remote_email"
    ynh_app_setting_set --key=remote_password --value="$current_remote_password"
    ynh_app_setting_set --key=remote_host --value="$remote_host"
    ynh_app_setting_set --key=remote_port --value="$remote_port"
    ynh_app_setting_set --key=delete_remote --value="$delete_remote"
    ynh_app_setting_set --key=target_user --value="$target_user"
    ynh_app_setting_set --key=local_imap_email --value="$resolved_target_email"
    ynh_app_setting_set --key=local_imap_password --value="$current_local_imap_password"
    ynh_app_setting_set --key=poll_interval_minutes --value="${poll_interval_minutes:-$default_poll_interval_minutes}"

    render_runtime_config "$resolved_target_email" "$current_remote_password" "$current_local_imap_password"
}

render_runtime_config() {
    local resolved_target_email="$1"
    local current_remote_password="$2"
    local current_local_imap_password="$3"
    local local_imap_email="$resolved_target_email"
    local remote_password="$current_remote_password"
    local local_imap_password="$current_local_imap_password"
    local poll_interval_minutes="${poll_interval_minutes:-$default_poll_interval_minutes}"

    ynh_config_add --template="config.yml" --destination="$runtime_config_path"
    chown "$app:$app" "$runtime_config_path"
    chmod 600 "$runtime_config_path"
}

render_timer_config() {
    local poll_interval_minutes="${poll_interval_minutes:-$(ynh_app_setting_get --key=poll_interval_minutes)}"
    poll_interval_minutes="${poll_interval_minutes:-$default_poll_interval_minutes}"

    ynh_config_add --template="systemd.timer" --destination="$timer_unit_path"
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

ensure_runtime_files() {
    if [ ! -f "$data_dir/state.json" ]; then
        initialize_state
    fi

    if [ -f "$runtime_config_path" ]; then
        chown "$app:$app" "$runtime_config_path"
        chmod 600 "$runtime_config_path"
    fi

    if [ -f "$data_dir/state.json" ]; then
        chown "$app:$app" "$data_dir/state.json"
        chmod 600 "$data_dir/state.json"
    fi
}

register_service() {
    yunohost service add "$app" \
        --description="$service_description" \
        --log="/var/log/$app/$app.log" \
        --test_status="systemctl is-enabled --quiet '$app.timer' && systemctl is-active --quiet '$app.timer'"
}

install_systemd_units() {
    render_timer_config
    ynh_config_add_systemd
    systemctl daemon-reload
    systemctl enable --quiet "$app.timer"
}

start_scheduler() {
    systemctl start "$app.service"
    systemctl start "$app.timer"
}

refresh_scheduler() {
    render_timer_config
    ynh_config_add_systemd
    systemctl daemon-reload
    systemctl restart "$app.timer"
}

stop_scheduler() {
    systemctl stop "$app.timer" >/dev/null 2>&1 || true
    systemctl stop "$app.service" >/dev/null 2>&1 || true
    systemctl disable --quiet "$app.timer" >/dev/null 2>&1 || true
}

remove_systemd_units() {
    stop_scheduler
    rm -f "$timer_unit_path"
    ynh_config_remove_systemd
    systemctl daemon-reload
}

resolve_target_email() {
    yunohost user info "$target_user" --output-as json 2>/dev/null \
        | tr -d '\n' \
        | sed -n 's/.*"mail"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}
