from django.db import migrations, models


def convert_core_tables_to_innodb(apps, schema_editor):
    """Make transaction.atomic() effective for legacy MyISAM imports."""
    connection = schema_editor.connection
    if connection.vendor != "mysql":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TABLE_NAME
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME LIKE %s
              AND ENGINE <> 'InnoDB'
            """,
            ["core\\_%"],
        )
        table_names = [row[0] for row in cursor.fetchall()]

        for table_name in table_names:
            quoted_name = connection.ops.quote_name(table_name)
            cursor.execute(f"ALTER TABLE {quoted_name} ENGINE=InnoDB")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_payrollbatch_finalized_at_payrollbatch_finalized_by_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="employment_start_date",
            field=models.DateField(
                blank=True,
                help_text="First date covered by the employee's appointment or contract.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="employment_end_date",
            field=models.DateField(
                blank=True,
                help_text="Last date covered by the employee's appointment or contract.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="holidaysuspension",
            name="is_payroll_verified",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Only verified entries affect DTR and payroll.",
            ),
        ),
        migrations.AddField(
            model_name="holidaysuspension",
            name="source_reference",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Official proclamation, memorandum, or suspension-order reference.",
                max_length=500,
            ),
        ),
        migrations.AlterField(
            model_name="employeecontribution",
            name="sss_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Monthly JO/COS SSS deduction. Leave at zero when not applicable.",
                max_digits=10,
            ),
        ),
        migrations.AlterField(
            model_name="employeecontribution",
            name="pagibig_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Monthly JO/COS amount or permanent per-period override.",
                max_digits=10,
            ),
        ),
        migrations.AlterField(
            model_name="employeecontribution",
            name="philhealth_value",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10),
        ),
        migrations.RunPython(
            convert_core_tables_to_innodb,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
