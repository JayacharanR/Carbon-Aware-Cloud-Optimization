// Budget deployment is intentionally separate from cluster deployment because
// a subscription budget needs a real notification address and a user-selected
// amount.  It is safe to run this file independently before provisioning AKS.
targetScope = 'subscription'

@description('Name used to identify this subscription budget.')
param budgetName string

@minValue(1)
@description('Monthly budget in the billing currency of the subscription.')
param monthlyAmount int

@description('At least one monitored notification email address.')
param contactEmails array

@description('UTC start date in ISO-8601 format, for example 2026-09-01T00:00:00Z.')
param startDate string

@description('UTC end date in ISO-8601 format. Choose a date after the research period.')
param endDate string

resource researchBudget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: monthlyAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
      endDate: endDate
    }
    notifications: {
      threshold50: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        contactEmails: contactEmails
      }
      threshold80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        contactEmails: contactEmails
      }
      threshold100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        contactEmails: contactEmails
      }
    }
  }
}
