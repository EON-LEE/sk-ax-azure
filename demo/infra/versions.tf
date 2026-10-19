terraform {
  required_version = ">= 1.9, < 2.0"
  backend "local" {}
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.60"
    }
  }
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
  storage_use_azuread             = true
  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}
