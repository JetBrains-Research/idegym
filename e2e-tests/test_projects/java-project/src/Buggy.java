/**
 * File with intentional issues for inspection testing.
 */
public class Buggy {

    // Unused local variable to trigger inspection
    public void unusedVariable() {
        int unused = 42;
        System.out.println("Method with unused variable");
    }

    // Method with unnecessary return to trigger inspection
    public void unnecessaryReturn() {
        System.out.println("Doing something");
        return;  // unnecessary return in void method
    }

    // Unchecked cast to trigger inspection
    @SuppressWarnings("rawtypes")
    public void uncheckedCast() {
        Object obj = "string";
        String str = (String) obj;  // unchecked cast
        System.out.println(str);
    }
}
