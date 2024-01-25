param adaModel string = 'text-embedding-ada-002'
param adaVersion string = '2'
param config string
param gptModel string = 'gpt-4'
param gptVersion string = '1106-Preview'
param imageVersion string = 'main'
param instance string = deployment().name
param location string = 'westeurope'
param openaiLocation string = 'swedencentral'
param searchLocation string = 'northeurope'

targetScope = 'subscription'

output appUrl string = app.outputs.appUrl
output blobStoragePublicName string = app.outputs.blobStoragePublicName
output communicationId string = app.outputs.communicationId

var tags = {
  application: 'claim-ai'
  instance: instance
  managed_by: 'Bicep'
  sources: 'https://github.com/clemlesne/claim-ai-phone-bot'
  version: imageVersion
}

resource sub 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  location: location
  name: instance
  tags: tags
}

module app 'app.bicep' = {
  name: instance
  scope: sub
  params: {
    adaModel: adaModel
    adaVersion: adaVersion
    config: config
    gptModel: gptModel
    gptVersion: gptVersion
    imageVersion: imageVersion
    location: location
    openaiLocation: openaiLocation
    searchLocation: searchLocation
    tags: tags
  }
}