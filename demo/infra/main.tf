locals {
  tags = { purpose = "axk2-demo", deployment = var.name }
}

resource "azurerm_resource_group" "demo" {
  name     = "rg-${var.name}"
  location = var.web_location
  tags     = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_account" "aml" {
  name                            = "${var.name}aml"
  resource_group_name             = azurerm_resource_group.demo.name
  location                        = var.gpu_location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  shared_access_key_enabled       = false
  public_network_access_enabled   = false
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
  tags                            = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_key_vault" "aml" {
  name                          = "${var.name}-kv"
  resource_group_name           = azurerm_resource_group.demo.name
  location                      = var.gpu_location
  tenant_id                     = var.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  public_network_access_enabled = false
  tags                          = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_log_analytics_workspace" "aml" {
  name                = "${var.name}-logs"
  resource_group_name = azurerm_resource_group.demo.name
  location            = var.gpu_location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.1
  tags                = local.tags
}

resource "azurerm_application_insights" "aml" {
  name                 = "${var.name}-ai"
  resource_group_name  = azurerm_resource_group.demo.name
  location             = var.gpu_location
  workspace_id         = azurerm_log_analytics_workspace.aml.id
  application_type     = "web"
  daily_data_cap_in_gb = 0.1
  tags                 = local.tags
}

resource "azurerm_machine_learning_workspace" "demo" {
  name                        = "${var.name}-mlw"
  resource_group_name         = azurerm_resource_group.demo.name
  location                    = var.gpu_location
  application_insights_id     = azurerm_application_insights.aml.id
  key_vault_id                = azurerm_key_vault.aml.id
  storage_account_id          = azurerm_storage_account.aml.id
  storage_account_access_type = "Identity"
  identity { type = "SystemAssigned" }
  tags = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_machine_learning_compute_cluster" "gpu" {
  name                          = "a100-nd96-lp"
  location                      = var.gpu_location
  machine_learning_workspace_id = azurerm_machine_learning_workspace.demo.id
  vm_priority                   = "LowPriority"
  vm_size                       = "Standard_ND96amsr_A100_v4"
  ssh_public_access_enabled     = false
  scale_settings {
    min_node_count                       = 0
    max_node_count                       = 3
    scale_down_nodes_after_idle_duration = "PT2M"
  }
  identity { type = "SystemAssigned" }
  tags = local.tags
}

resource "azurerm_role_assignment" "compute_storage" {
  scope                = azurerm_storage_account.aml.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_machine_learning_compute_cluster.gpu.identity[0].principal_id
}

resource "azurerm_service_plan" "web" {
  name                = "${var.name}-plan"
  resource_group_name = azurerm_resource_group.demo.name
  location            = var.web_location
  os_type             = "Linux"
  sku_name            = var.web_sku
  worker_count        = 1
  tags                = local.tags
}

resource "azurerm_linux_web_app" "demo" {
  name                                           = var.name
  resource_group_name                            = azurerm_resource_group.demo.name
  location                                       = var.web_location
  service_plan_id                                = azurerm_service_plan.web.id
  https_only                                     = true
  public_network_access_enabled                  = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  identity { type = "SystemAssigned" }
  site_config {
    always_on           = true
    websockets_enabled  = true
    http2_enabled       = true
    minimum_tls_version = "1.2"
    ftps_state          = "Disabled"
    app_command_line    = "python -m uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers"
    application_stack { python_version = "3.12" }
  }
  app_settings = {
    SCM_DO_BUILD_DURING_DEPLOYMENT = "true"
    FORWARDED_ALLOW_IPS            = "*"
    WEBSITES_PORT                  = "8000"
    AXK2_DATA                      = "/home/data"
    AXK2_PUBLIC_URL                = "https://${var.name}.azurewebsites.net"
    AXK2_SUBSCRIPTION              = var.subscription_id
    AXK2_RESOURCE_GROUP            = azurerm_resource_group.demo.name
    AXK2_WORKSPACES                = "${var.gpu_location}=${azurerm_machine_learning_workspace.demo.name}"
    AXK2_COMPUTE                   = azurerm_machine_learning_compute_cluster.gpu.name
    AXK2_OPEN_DEMO                 = "1"
    AXK2_DEPLOYMENT                = var.name
  }
  tags = local.tags
  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      app_settings["AXK2_SESSION_SECRET"],
      app_settings["AXK2_ADMIN_PASSWORD_HASH"],
      app_settings["AXK2_DEMO_PASSWORD_HASH"],
      app_settings["AXK2_WEBIQ_ENDPOINT"],
      app_settings["AXK2_WEBIQ_API_KEY"],
      app_settings["AXK2_WEBIQ_ACCESS_APPROVED"],
      app_settings["AXK2_WEBIQ_TOOL"],
      app_settings["AXK2_WEBIQ_QUERY_FIELD"],
      app_settings["AXK2_WEBIQ_ARGUMENTS_JSON"],
      app_settings["AXK2_WEBIQ_BEARER_TOKEN"],
      app_settings["AXK2_MODEL_API_KEY_SHA256"],
    ]
  }
}

resource "azurerm_role_assignment" "web_aml" {
  scope                = azurerm_machine_learning_workspace.demo.id
  role_definition_name = "AzureML Data Scientist"
  principal_id         = azurerm_linux_web_app.demo.identity[0].principal_id
}

output "deployment" {
  value = {
    name           = var.name
    resource_group = azurerm_resource_group.demo.name
    workspace      = azurerm_machine_learning_workspace.demo.name
    gpu_location   = var.gpu_location
    compute        = azurerm_machine_learning_compute_cluster.gpu.name
    app            = azurerm_linux_web_app.demo.name
    url            = "https://${azurerm_linux_web_app.demo.default_hostname}"
  }
}
