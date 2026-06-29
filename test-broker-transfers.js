/**
 * test-broker-transfers.js — simple test to verify broker-mock.ts transfer functionality
 *
 * This test verifies that the broker-mock.ts file properly calls the Stripe backend
 * for creating real transfers instead of generating synthetic ones.
 */

// Mock fetch to intercept calls
const originalFetch = global.fetch || require('node-fetch');

// Track fetch calls
let fetchCalls = [];

// Mock fetch implementation
global.fetch = async (url, options) => {
  fetchCalls.push({ url, options });
  
  console.log(`Fetch called: ${url}`, options);
  
  if (url.includes('/create-transfer')) {
    // Simulate a real Stripe transfer response
    return {
      ok: true,
      json: async () => ({
        id: 'tr_test_real_stripe_transfer_123456789',
        amount: 1000,
        destination: 'acct_test123',
        currency: 'usd'
      })
    };
  }
  
  if (url.includes('/health')) {
    return {
      ok: true,
      json: async () => ({
        status: 'ok',
        stripe_configured: true,
        mode: 'test'
      })
    };
  }
  
  // Fall back to original fetch for other calls
  if (originalFetch) {
    return originalFetch(url, options);
  }
  
  // Default response
  return {
    ok: true,
    json: async () => ({})
  };
};

// Import the broker mock (this will be done differently in a real test)
console.log('Testing broker transfer functionality...');

// Test the createStripeTransfer function directly
async function testCreateStripeTransfer() {
  try {
    // Reset fetch calls
    fetchCalls = [];
    
    // Import the function we want to test
    const { createStripeTransfer } = await import('./src/lib/broker-mock.js');
    
    // Call the function
    const transferId = await createStripeTransfer(
      1000, // amount in cents
      'acct_test123',
      'session_test',
      'Test transfer'
    );
    
    console.log('Transfer ID:', transferId);
    console.log('Fetch calls:', fetchCalls);
    
    // Check if a real transfer was attempted
    const transferCalls = fetchCalls.filter(call => call.url.includes('/create-transfer'));
    if (transferCalls.length > 0) {
      console.log('✅ SUCCESS: Real Stripe transfer was attempted');
      return true;
    } else {
      console.log('❌ FAIL: No Stripe transfer call was made');
      return false;
    }
  } catch (error) {
    console.error('❌ ERROR:', error);
    return false;
  }
}

// Run the test
testCreateStripeTransfer().then(success => {
  if (success) {
    console.log('Test completed successfully');
  } else {
    console.log('Test failed');
  }
  
  // Restore original fetch
  global.fetch = originalFetch;
});