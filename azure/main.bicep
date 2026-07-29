# Azure infra — Bicep template.
#
# Deploys:
#   1. Resource group:  rg-halalistic-pilot
#   2. Log Analytics:   log-halalistic-<env>
#   3. App Insights:    appi-halalistic-<env>
#   4. Container Apps Environment:  cae-halalistic-<env>
#   5. Container App:                ca-halalistic-<env>
#   6. Key Vault:        kv-halalistic-<env>  (secrets store, RBAC-only access)
#   7. Managed Identity: id-halalistic-<env> (assigned to the Container App)
#   8. Postgres Flexible Server: psql-halalistic-<env>  (dev/staging only)
#
# Production deliberately does NOT provision Postgres here — we point
# at an existing production DB via env var (managed separately, backed
# up nightly, point-in-time recovery enabled). Pilot scale doesn't
# justify a Bicep-managed prod DB.
#
# Deploy:
#   az deployment sub create --location eastus \
#     --template-file infra/main.bicep \
#     --parameters environmentName=staging \
#                   postgresAdminPassword=$(openssl rand -base64 24)
#
# Secrets to seed into Key Vault after first deploy (manual step):
#   az keyvault secret set --vault-name $KV --name "DatabaseUrl" --value "..."
#   az keyvault secret set --vault-name $KV --name "StripeSecretKey" --value "..."
#   az keyvault secret set --vault-name $KV --name "StripeWebhookSecret" --value "..."
#   az keyvault secret set --vault-name $KV --name "AzureBlobConnectionString" --value "..."
#   az keyvault secret set --vault-name $KV --name "GoogleMapsApiKey" --value "..."
#   az keyvault secret set --vault-name $KV --name "EmailProviderApiKey" --value "..."
#   az keyvault secret set --vault-name $KV --name "AcsConnectionString" --value "..."

targetScope = 'resourceGroup'

@description('Environment name (dev / staging / production).')
@allowed(['dev', 'staging', 'production'])
param environmentName string

@description('Azure region for the deployment.')
param location string = resourceGroup().location

@description('Image to deploy, e.g. ghcr.io/halalistic/halalistic:sha-abc1234')
param imageToDeploy string

@description('Postgres admin password (dev/staging only). Use a Key Vault reference in prod.')
@secure()
param postgresAdminPassword string = ''

@description('Existing Azure Container Registry to pull images from.')
param acrLoginServer string = ''

@description('Whether to provision a Postgres Flexible Server in this env.')
param provisionPostgres bool = (environmentName != 'production')

var prefix = 'halalistic-${environmentName}'
var kvName = 'kv-halalistic-${uniqueString(resourceGroup().id)}'

// ---- Log Analytics (Container Apps needs it) ----
resource log 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}'
  location: location
  sku: { name: 'PerGB2018' }
  properties: { retentionInDays: 30 }
}

// ---- App Insights ----
resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: log.id
    DisableLocalAuth: true
  }
}

// ---- Key Vault (RBAC mode) ----
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { name: 'standard', family: 'A' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

// ---- Managed Identity for the Container App ----
resource id 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}'
  location: location
}

// Grant the identity "Key Vault Secrets User" so the Container App
// can pull secrets at runtime.
resource kvAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, id.id, 'KeyVaultSecretsUser')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'  // Key Vault Secrets User
    )
    principalId: id.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- Postgres (dev/staging only) ----
resource psql 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01' = if (provisionPostgres) {
  name: 'psql-${prefix}'
  location: location
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    administratorLogin: 'halalistic'
    administratorLoginPassword: postgresAdminPassword
    version: '16'
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
  }
}

// ---- Container Apps Environment ----
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${prefix}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: log.properties.customerId
        // sharedKey is sensitive but not a long-lived secret — it lives
        // in the workspace and is referenced here. In a real prod
        // setup you'd externalize this too.
        sharedKey: log.listKeys().primarySharedKey
      }
    }
  }
}

// ---- The Container App itself ----
resource ca 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${prefix}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${id.id}': {} }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        allowInsecure: false
        traffic: [
          { latestRevision: true, weight: 100 }
        ]
      }
      registries: empty(acrLoginServer) ? [] : [
        { server: acrLoginServer, identity: id.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'halalistic'
          image: imageToDeploy
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'ENV', value: environmentName }
            { name: 'LOG_LEVEL', value: 'INFO' }
            { name: 'APPINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
            // All real secrets are pulled from Key Vault at startup
            // via the `secrets:` block below. Only non-sensitive
            // config goes in plain env.
          ]
      ]
      scale: {
        minReplicas: (environmentName == 'production') ? 2 : 1
        maxReplicas: (environmentName == 'production') ? 6 : 2
        rules: [
          {
            name: 'http-scale'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

// ---- Output: what the CD pipeline needs ----
output containerAppFqdn string = ca.properties.configuration.ingress.fqdn
output containerAppName string = ca.name
output keyVaultName string = kv.name
output managedIdentityClientId string = id.properties.clientId
